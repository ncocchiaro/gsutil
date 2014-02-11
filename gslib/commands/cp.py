# Copyright 2011 Google Inc. All Rights Reserved.
# Copyright 2011, Nexenta Systems Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Implementation of Unix-like cp command for cloud storage providers."""

# Get the system logging module, not our local logging module.
from __future__ import absolute_import

import logging
import os
import time
import traceback

from gslib.bucket_listing_ref import BucketListingRef
from gslib.bucket_listing_ref import BucketListingRefType
from gslib.cat_helper import CatHelper
from gslib.cloud_api import AccessDeniedException
from gslib.cloud_api import NotFoundException
from gslib.command import Command
from gslib.commands.compose import MAX_COMPONENT_COUNT
from gslib.copy_helper import CopyHelper
from gslib.copy_helper import CreateOptsTuple
from gslib.copy_helper import GetOptsTuple
from gslib.copy_helper import ItemExistsError
from gslib.copy_helper import Manifest
from gslib.copy_helper import PARALLEL_UPLOAD_TEMP_NAMESPACE
from gslib.cs_api_map import ApiSelector
from gslib.exception import CommandException
from gslib.name_expansion import NameExpansionIterator
from gslib.name_expansion import NameExpansionResult
from gslib.storage_url import ContainsWildcard
from gslib.storage_url import StorageUrlFromString
from gslib.util import CreateLock
from gslib.util import GetCloudApiInstance
from gslib.util import MakeHumanReadable
from gslib.util import NO_MAX

SYNOPSIS_TEXT = """
<B>SYNOPSIS</B>
  gsutil cp [OPTION]... src_url dst_url
  gsutil cp [OPTION]... src_url... dst_url
  gsutil cp [OPTION]... -I dst_url
"""

DESCRIPTION_TEXT = """
<B>DESCRIPTION</B>
  The gsutil cp command allows you to copy data between your local file
  system and the cloud, copy data within the cloud, and copy data between
  cloud storage providers. For example, to copy all text files from the
  local directory to a bucket you could do:

    gsutil cp *.txt gs://my_bucket

  Similarly, you can download text files from a bucket by doing:

    gsutil cp gs://my_bucket/*.txt .

  If you want to copy an entire directory tree you need to use the -R option:

    gsutil cp -R dir gs://my_bucket

  If you have a large number of files to upload you might want to use the
  gsutil -m option, to perform a parallel (multi-threaded/multi-processing)
  copy:

    gsutil -m cp -R dir gs://my_bucket

  You can pass a list of URLs to copy on STDIN instead of as command line
  arguments by using the -I option. This allows you to use gsutil in a
  pipeline to copy files and objects as generated by a program, such as:

    some_program | gsutil -m cp -I gs://my_bucket

  The contents of STDIN can name files, cloud URLs, and wildcards of files
  and cloud URLs.
"""

NAME_CONSTRUCTION_TEXT = """
<B>HOW NAMES ARE CONSTRUCTED</B>
  The gsutil cp command strives to name objects in a way consistent with how
  Linux cp works, which causes names to be constructed in varying ways depending
  on whether you're performing a recursive directory copy or copying
  individually named objects; and whether you're copying to an existing or
  non-existent directory.

  When performing recursive directory copies, object names are constructed
  that mirror the source directory structure starting at the point of
  recursive processing. For example, the command:

    gsutil cp -R dir1/dir2 gs://my_bucket

  will create objects named like gs://my_bucket/dir2/a/b/c, assuming
  dir1/dir2 contains the file a/b/c.

  In contrast, copying individually named files will result in objects named
  by the final path component of the source files. For example, the command:

    gsutil cp dir1/dir2/** gs://my_bucket

  will create objects named like gs://my_bucket/c.

  The same rules apply for downloads: recursive copies of buckets and
  bucket subdirectories produce a mirrored filename structure, while copying
  individually (or wildcard) named objects produce flatly named files.

  Note that in the above example the '**' wildcard matches all names
  anywhere under dir. The wildcard '*' will match names just one level deep. For
  more details see 'gsutil help wildcards'.

  There's an additional wrinkle when working with subdirectories: the resulting
  names depend on whether the destination subdirectory exists. For example,
  if gs://my_bucket/subdir exists as a subdirectory, the command:

    gsutil cp -R dir1/dir2 gs://my_bucket/subdir

  will create objects named like gs://my_bucket/subdir/dir2/a/b/c. In contrast,
  if gs://my_bucket/subdir does not exist, this same gsutil cp command will
  create objects named like gs://my_bucket/subdir/a/b/c.
"""

SUBDIRECTORIES_TEXT = """
<B>COPYING TO/FROM SUBDIRECTORIES; DISTRIBUTING TRANSFERS ACROSS MACHINES</B>
  You can use gsutil to copy to and from subdirectories by using a command
  like:

    gsutil cp -R dir gs://my_bucket/data

  This will cause dir and all of its files and nested subdirectories to be
  copied under the specified destination, resulting in objects with names like
  gs://my_bucket/data/dir/a/b/c. Similarly you can download from bucket
  subdirectories by using a command like:

    gsutil cp -R gs://my_bucket/data dir

  This will cause everything nested under gs://my_bucket/data to be downloaded
  into dir, resulting in files with names like dir/data/a/b/c.

  Copying subdirectories is useful if you want to add data to an existing
  bucket directory structure over time. It's also useful if you want
  to parallelize uploads and downloads across multiple machines (often
  reducing overall transfer time compared with simply running gsutil -m
  cp on one machine). For example, if your bucket contains this structure:

    gs://my_bucket/data/result_set_01/
    gs://my_bucket/data/result_set_02/
    ...
    gs://my_bucket/data/result_set_99/

  you could perform concurrent downloads across 3 machines by running these
  commands on each machine, respectively:

    gsutil -m cp -R gs://my_bucket/data/result_set_[0-3]* dir
    gsutil -m cp -R gs://my_bucket/data/result_set_[4-6]* dir
    gsutil -m cp -R gs://my_bucket/data/result_set_[7-9]* dir

  Note that dir could be a local directory on each machine, or it could
  be a directory mounted off of a shared file server; whether the latter
  performs acceptably may depend on a number of things, so we recommend
  you experiment and find out what works best for you.
"""

COPY_IN_CLOUD_TEXT = """
<B>COPYING IN THE CLOUD AND METADATA PRESERVATION</B>
  If both the source and destination URL are cloud URLs from the same
  provider, gsutil copies data "in the cloud" (i.e., without downloading
  to and uploading from the machine where you run gsutil). In addition to
  the performance and cost advantages of doing this, copying in the cloud
  preserves metadata (like Content-Type and Cache-Control).  In contrast,
  when you download data from the cloud it ends up in a file, which has
  no associated metadata. Thus, unless you have some way to hold on to
  or re-create that metadata, downloading to a file will not retain the
  metadata.

  Note that by default, the gsutil cp command does not copy the object
  ACL to the new object, and instead will use the default bucket ACL (see
  "gsutil help defacl").  You can override this behavior with the -p
  option (see OPTIONS below).
"""

RESUMABLE_TRANSFERS_TEXT = """
<B>RESUMABLE TRANSFERS</B>
  gsutil automatically uses the Google Cloud Storage resumable upload
  feature whenever you use the cp command to upload an object that is larger
  than 2 MB. You do not need to specify any special command line options
  to make this happen. If your upload is interrupted you can restart the
  upload by running the same cp command that you ran to start the upload.

  Similarly, gsutil automatically performs resumable downloads (using HTTP
  standard Range GET operations) whenever you use the cp command to download an
  object larger than 2 MB.

  Resumable uploads and downloads store some state information in a file
  in ~/.gsutil named by the destination object or file. If you attempt to
  resume a transfer from a machine with a different directory, the transfer
  will start over from scratch.

  See also "gsutil help prod" for details on using resumable transfers
  in production.
"""

STREAMING_TRANSFERS_TEXT = """
<B>STREAMING TRANSFERS</B>
  Use '-' in place of src_url or dst_url to perform a streaming
  transfer. For example:

    long_running_computation | gsutil cp - gs://my_bucket/obj

  Streaming transfers do not support resumable uploads/downloads.
  (The Google resumable transfer protocol has a way to support streaming
  transfers, but gsutil doesn't currently implement support for this.)
"""

PARALLEL_COMPOSITE_UPLOADS_TEXT = """
<B>PARALLEL COMPOSITE UPLOADS</B>
  gsutil automatically uses
  `object composition <https://developers.google.com/storage/docs/composite-objects>`_
  to perform uploads in parallel for large, local files being uploaded to
  Google Cloud Storage. This means that, by default, a large file will be split
  into component pieces that will be uploaded in parallel. Those components will
  then be composed in the cloud, and the temporary components in the cloud will
  be deleted after successful composition. No additional local disk space is
  required for this operation.

  Any file whose size exceeds the "parallel_composite_upload_threshold" config
  variable will trigger this feature by default. The ideal size of a
  component can also be set with the "parallel_composite_upload_component_size"
  config variable. See the .boto config file for details about how these values
  are used.

  If the transfer fails prior to composition, running the command again will
  take advantage of resumable uploads for those components that failed, and
  the component objects will be deleted after the first successful attempt.
  Any temporary objects that were uploaded successfully before gsutil failed
  will still exist until the upload is completed successfully. The temporary
  objects will be named in the following fashion:
  <random ID>%s<hash>
  where <random ID> is some numerical value, and <hash> is an MD5 hash (not
  related to the hash of the contents of the file or object).

  One important caveat is that files uploaded in this fashion are still subject
  to the maximum number of components limit. For example, if you upload a large
  file that gets split into %d components, and try to compose it with another
  object with %d components, the operation will fail because it exceeds the %d
  component limit. If you wish to compose an object later and the component
  limit is a concern, it is recommended that you disable parallel composite
  uploads for that transfer.

  Also note that an object uploaded using this feature will have a CRC32C hash,
  but it will not have an MD5 hash. For details see 'gsutil help crc32c'.

  Note that this feature can be completely disabled by setting the
  "parallel_composite_upload_threshold" variable in the .boto config file to 0.
""" % (PARALLEL_UPLOAD_TEMP_NAMESPACE, 10, MAX_COMPONENT_COUNT - 9,
       MAX_COMPONENT_COUNT)

CHANGING_TEMP_DIRECTORIES_TEXT = """
<B>CHANGING TEMP DIRECTORIES</B>
  gsutil writes data to a temporary directory in several cases:

  - when compressing data to be uploaded (see the -z option)
  - when decompressing data being downloaded (when the data has
    Content-Encoding:gzip, e.g., as happens when uploaded using gsutil cp -z)
  - when running integration tests (using the gsutil test command)

  In these cases it's possible the temp file location on your system that
  gsutil selects by default may not have enough space. If you find that
  gsutil runs out of space during one of these operations (e.g., raising
  "CommandException: Inadequate temp space available to compress <your file>"
  during a gsutil cp -z operation), you can change where it writes these
  temp files by setting the TMPDIR environment variable. On Linux and MacOS
  you can do this either by running gsutil this way:

    TMPDIR=/some/directory gsutil cp ...

  or by adding this line to your ~/.bashrc file and then restarting the shell
  before running gsutil:

    export TMPDIR=/some/directory

  On Windows 7 you can change the TMPDIR environment variable from Start ->
  Computer -> System -> Advanced System Settings -> Environment Variables.
  You need to reboot after making this change for it to take effect. (Rebooting
  is not necessary after running the export command on Linux and MacOS.)
"""

OPTIONS_TEXT = """
<B>OPTIONS</B>
  -a canned_acl  Sets named canned_acl when uploaded objects created. See
                 'gsutil help acls' for further details.

  -c            If an error occurrs, continue to attempt to copy the remaining
                files. Note that this option is always true when running
                "gsutil -m cp".

  -D            Copy in "daisy chain" mode, i.e., copying between two buckets by
                hooking a download to an upload, via the machine where gsutil is
                run. By default, data are copied between two buckets
                "in the cloud", i.e., without needing to copy via the machine
                where gsutil runs.

                By default, a "copy in the cloud" when the source is a composite
                object will retain the composite nature of the object. However,
                Daisy chain mode can be used to change a composite object into
                a non-composite object. For example:

                    gsutil cp -D -p gs://bucket/obj gs://bucket/obj_tmp
                    gsutil mv -p gs://bucket/obj_tmp gs://bucket/obj

                Note: Daisy chain mode is automatically used when copying
                between providers (e.g., to copy data from Google Cloud Storage
                to another provider).

  -e            Exclude symlinks. When specified, symbolic links will not be
                copied.

  -L <file>     Outputs a manifest log file with detailed information about each
                item that was copied. This manifest contains the following
                information for each item:

                - Source path.
                - Destination path.
                - Source size.
                - Bytes transferred.
                - MD5 hash.
                - UTC date and time transfer was started in ISO 8601 format.
                - UTC date and time transfer was completed in ISO 8601 format.
                - Upload id, if a resumable upload was performed.
                - Final result of the attempted transfer, success or failure.
                - Failure details, if any.

                If the log file already exists, gsutil will use the file as an
                input to the copy process, and will also append log items to the
                existing file. Files/objects that are marked in the existing log
                file as having been successfully copied (or skipped) will be
                ignored. Files/objects without entries will be copied and ones
                previously marked as unsuccessful will be retried. This can be
                used in conjunction with the -c option to build a script that
                copies a large number of objects reliably, using a bash script
                like the following:

                    status=1
                    while [ $status -ne 0 ] ; do
                        gsutil cp -c -L cp.log -R ./dir gs://bucket
                        status=$?
                    done

                The -c option will cause copying to continue after failures
                occur, and the -L option will allow gsutil to pick up where it
                left off without duplicating work. The loop will continue
                running as long as gsutil exits with a non-zero status (such a
                status indicates there was at least one failure during the
                gsutil run).

  -n            No-clobber. When specified, existing files or objects at the
                destination will not be overwritten. Any items that are skipped
                by this option will be reported as being skipped. This option
                will perform an additional GET request to check if an item
                exists before attempting to upload the data. This will save
                retransmitting data, but the additional HTTP requests may make
                small object transfers slower and more expensive.

  -p            Causes ACLs to be preserved when copying in the cloud. Note that
                this option has performance and cost implications when using 
                the XML API, as it requires separate HTTP calls for interacting
                with ACLs. The performance issue can be mitigated to some
                degree by using gsutil -m cp to cause parallel copying.)

                You can avoid the additional performance and cost of using cp -p
                if you want all objects in the destination bucket to end up with
                the same ACL by setting a default ACL on that bucket instead of
                using cp -p. See "help gsutil defacl".

                Note that it's not valid to specify both the -a and -p options
                together.

  -q            Deprecated. Please use gsutil -q cp ... instead.

  -R, -r        Causes directories, buckets, and bucket subdirectories to be
                copied recursively. If you neglect to use this option for
                an upload, gsutil will copy any files it finds and skip any
                directories. Similarly, neglecting to specify -R for a download
                will cause gsutil to copy any objects at the current bucket
                directory level, and skip any subdirectories.

  -v            Requests that the version-specific URL for each uploaded object
                be printed. Given this URL you can make future upload requests
                that are safe in the face of concurrent updates, because Google
                Cloud Storage will refuse to perform the update if the current
                object version doesn't match the version-specific URL. See
                'gsutil help versions' for more details.

  -z <ext,...>  Applies gzip content-encoding to file uploads with the given
                extensions. This is useful when uploading files with
                compressible content (such as .js, .css, or .html files) because
                it saves network bandwidth and space in Google Cloud Storage,
                which in turn reduces storage costs.

                When you specify the -z option, the data from your files is
                compressed before it is uploaded, but your actual files are left
                uncompressed on the local disk. The uploaded objects retain the
                Content-Type and name of the original files but are given a
                Content-Encoding header with the value "gzip" to indicate that
                the object data stored are compressed on the Google Cloud
                Storage servers.

                For example, the following command:

                  gsutil cp -z html -a public-read cattypes.html gs://mycats

                will do all of the following:

                - Upload as the object gs://mycats/cattypes.html (cp command)
                - Set the Content-Type to text/html (based on file extension)
                - Compress the data in the file cattypes.html (-z option)
                - Set the Content-Encoding to gzip (-z option)
                - Set the ACL to public-read (-a option)
                - If a user tries to view cattypes.html in a browser, the
                  browser will know to uncompress the data based on the
                  Content-Encoding header, and to render it as HTML based on
                  the Content-Type header.
"""

_detailed_help_text = '\n\n'.join([SYNOPSIS_TEXT,
                                   DESCRIPTION_TEXT,
                                   NAME_CONSTRUCTION_TEXT,
                                   SUBDIRECTORIES_TEXT,
                                   COPY_IN_CLOUD_TEXT,
                                   RESUMABLE_TRANSFERS_TEXT,
                                   STREAMING_TRANSFERS_TEXT,
                                   PARALLEL_COMPOSITE_UPLOADS_TEXT,
                                   CHANGING_TEMP_DIRECTORIES_TEXT,
                                   OPTIONS_TEXT])


CP_SUB_ARGS = 'a:cDeIL:MNnpqrRtvz:'


def _CopyFuncWrapper(cls, args, thread_state=None):
  cls.CopyFunc(args, thread_state=thread_state)


def _CopyExceptionHandler(cls, e):
  """Simple exception handler to allow post-completion status."""
  cls.logger.error(str(e))
  cls.copy_failure_count += 1
  cls.logger.debug('\n\nEncountered exception while copying:\n%s\n' %
                   traceback.format_exc())


def _RmExceptionHandler(cls, e):
  """Simple exception handler to allow post-completion status."""
  cls.logger.error(str(e))


class CpCommand(Command):
  """Implementation of gsutil cp command.

  Note that CpCommand is run for both gsutil cp and gsutil mv. The latter
  happens by MvCommand calling CpCommand and passing the hidden (undocumented)
  -M option. This allows the copy and remove needed for each mv to run
  together (rather than first running all the cp's and then all the rm's, as
  we originally had implemented), which in turn avoids the following problem
  with removing the wrong objects: starting with a bucket containing only
  the object gs://bucket/obj, say the user does:
    gsutil mv gs://bucket/* gs://bucket/d.txt
  If we ran all the cp's and then all the rm's and we didn't expand the wildcard
  first, the cp command would first copy gs://bucket/obj to gs://bucket/d.txt,
  and the rm command would then remove that object. In the implementation
  prior to gsutil release 3.12 we avoided this by building a list of objects
  to process and then running the copies and then the removes; but building
  the list up front limits scalability (compared with the current approach
  of processing the bucket listing iterator on the fly).
  """

  # Command specification. See base class for documentation.
  command_spec = Command.CreateCommandSpec(
      'cp',
      command_name_aliases=['copy'],
      min_args=1,
      max_args=NO_MAX,
      # -t is deprecated but leave intact for now to avoid breakage.
      supported_sub_args=CP_SUB_ARGS,
      file_url_ok=True,
      provider_url_ok=False,
      urls_start_arg=0,
      gs_api_support=[ApiSelector.XML, ApiSelector.JSON],
      gs_default_api=ApiSelector.JSON,
  )
  # Help specification. See help_provider.py for documentation.
  help_spec = Command.HelpSpec(
      help_name='cp',
      help_name_aliases=['copy'],
      help_type='command_help',
      help_one_line_summary='Copy files and objects',
      help_text=_detailed_help_text,
      subcommand_help_text={},
  )

  def CopyFunc(self, name_expansion_result, thread_state=None):
    """Worker function for performing the actual copy (and rm, for mv)."""
    gsutil_api = GetCloudApiInstance(self, thread_state=thread_state)
    exp_dst_url = self.exp_dst_url
    copy_helper = self.copy_helper
    have_existing_dst_container = self.have_existing_dst_container

    opts_tuple = GetOptsTuple()
    if opts_tuple.perform_mv:
      cmd_name = 'mv'
    else:
      cmd_name = self.command_name
    src_url_str = name_expansion_result.GetSrcUrlStr()
    src_url = StorageUrlFromString(src_url_str)
    exp_src_url_str = name_expansion_result.GetExpandedUrlStr()
    exp_src_url = StorageUrlFromString(exp_src_url_str)
    src_url_names_container = name_expansion_result.NamesContainer()
    src_url_expands_to_multi = name_expansion_result.NamesContainer()
    have_multiple_srcs = name_expansion_result.IsMultiSrcRequest()
    have_existing_dest_subdir = (
        name_expansion_result.HaveExistingDstContainer())

    if src_url.IsCloudUrl() and src_url.IsProvider():
      raise CommandException(
          'The %s command does not allow provider-only source URLs (%s)' %
          (cmd_name, src_url))
    if have_multiple_srcs:
      copy_helper.InsistDstUrlNamesContainer(
          exp_dst_url, have_existing_dst_container, cmd_name)

    if opts_tuple.use_manifest and self.manifest.WasSuccessful(
        exp_src_url.GetUrlString()):
      return

    if opts_tuple.perform_mv:
      if name_expansion_result.NamesContainer():
        # Use recursion_requested when performing name expansion for the
        # directory mv case so we can determine if any of the source URLs are
        # directories (and then use cp -R and rm -R to perform the move, to
        # match the behavior of Linux mv (which when moving a directory moves
        # all the contained files).
        self.recursion_requested = True
        # Disallow wildcard src URLs when moving directories, as supporting it
        # would make the name transformation too complex and would also be
        # dangerous (e.g., someone could accidentally move many objects to the
        # wrong name, or accidentally overwrite many objects).
        if ContainsWildcard(src_url_str):
          raise CommandException('The mv command disallows naming source '
                                 'directories using wildcards')

    if (exp_dst_url.IsFileUrl()
        and not os.path.exists(exp_dst_url.object_name)
        and have_multiple_srcs):
      os.makedirs(exp_dst_url.object_name)

    dst_url = copy_helper.ConstructDstUrl(
        src_url, exp_src_url, src_url_names_container, src_url_expands_to_multi,
        have_multiple_srcs, exp_dst_url, have_existing_dest_subdir)
    dst_url = copy_helper.FixWindowsNaming(src_url, dst_url)

    copy_helper.CheckForDirFileConflict(exp_src_url, dst_url)
    if copy_helper.SrcDstSame(exp_src_url, dst_url):
      raise CommandException('%s: "%s" and "%s" are the same file - '
                             'abort.' % (cmd_name,
                                         exp_src_url.GetUrlString(),
                                         dst_url.GetUrlString()))

    if dst_url.IsCloudUrl() and dst_url.HasGeneration():
      raise CommandException('%s: a version-specific URL\n(%s)\ncannot be '
                             'the destination for gsutil cp - abort.'
                             % (cmd_name, dst_url.GetUrlString()))

    elapsed_time = bytes_transferred = 0
    try:
      if opts_tuple.use_manifest:
        self.manifest.Initialize(
            exp_src_url.GetUrlString(), dst_url.GetUrlString())
      (elapsed_time, bytes_transferred, result_url, md5) = (
          copy_helper.PerformCopy(exp_src_url, dst_url, gsutil_api))
      if opts_tuple.use_manifest:
        if md5:
          self.manifest.Set(exp_src_url.GetUrlString(), 'md5', md5)
        self.manifest.SetResult(
            exp_src_url.GetUrlString(), bytes_transferred, 'OK')
    except ItemExistsError:
      message = 'Skipping existing item: %s' % dst_url.GetUrlString()
      self.logger.info(message)
      if opts_tuple.use_manifest:
        self.manifest.SetResult(exp_src_url.GetUrlString(), 0, 'skip', message)
    except Exception, e:
      if copy_helper.IsNoClobberServerException(e):
        message = 'Rejected (noclobber): %s' % dst_url.GetUrlString()
        self.logger.info(message)
        if opts_tuple.use_manifest:
          self.manifest.SetResult(
              exp_src_url.GetUrlString(), 0, 'skip', message)
      elif self.continue_on_error:
        message = 'Error copying %s: %s' % (src_url.GetUrlString(), str(e))
        self.copy_failure_count += 1
        self.logger.error(message)
        if opts_tuple.use_manifest:
          self.manifest.SetResult(
              exp_src_url.GetUrlString(), 0, 'error', message)
      else:
        if opts_tuple.use_manifest:
          self.manifest.SetResult(
              exp_src_url.GetUrlString(), 0, 'error', str(e))
        raise

    if opts_tuple.print_ver:
      # Some cases don't return a version-specific URL (e.g., if destination
      # is a file).
      self.logger.info('Created: %s' % result_url.GetUrlString())

    if opts_tuple.canned_acl:
      # Package up destination URL in a NameExpansionResult so SetAclFunc
      # can operate on it.  All that is used is the blr to get the URL string.
      dst_blr = BucketListingRef(dst_url.GetUrlString(),
                                 BucketListingRefType.OBJECT)
      dst_name_ex_result = NameExpansionResult('', False, False, False, dst_blr,
                                               have_existing_dst_container=None)
      self.SetAclFunc(dst_name_ex_result, thread_state=thread_state)

    # TODO: If we ever use -n (noclobber) with -M (move) (not possible today
    # since we call copy internally from move and don't specify the -n flag)
    # we'll need to only remove the source when we have not skipped the
    # destination.
    if opts_tuple.perform_mv:
      self.logger.info('Removing %s...', exp_src_url)
      if exp_src_url.IsCloudUrl():
        gsutil_api.DeleteObject(exp_src_url.bucket_name,
                                exp_src_url.object_name,
                                generation=exp_src_url.generation,
                                provider=exp_src_url.scheme)
      else:
        os.unlink(exp_src_url.object_name)

    with self.stats_lock:
      self.total_elapsed_time += elapsed_time
      self.total_bytes_transferred += bytes_transferred


  # Command entry point.
  def RunCommand(self):
    opts_tuple = self._ParseOpts()
    self.copy_helper = CopyHelper(
        command_obj=self, command_name=self.command_name, args=self.args,
        opts_tuple=opts_tuple, sub_opts=self.sub_opts, headers=self.headers,
        logger=self.logger, manifest=self.manifest,
        copy_exception_handler=_CopyExceptionHandler,
        rm_exception_handler=_RmExceptionHandler)

    self.total_elapsed_time = self.total_bytes_transferred = 0
    if self.args[-1] == '-' or self.args[-1] == 'file://-':
      return CatHelper(self).CatUrlStrings(self.args[:-1])

    if opts_tuple.read_args_from_stdin:
      if len(self.args) != 1:
        raise CommandException('Source URLs cannot be specified with -I option')
      url_strs = self.copy_helper.StdinIterator()
    else:
      if len(self.args) < 2:
        raise CommandException('Wrong number of arguments for "cp" command.')
      url_strs = self.args[:-1]

    (exp_dst_url, have_existing_dst_container) = self.copy_helper.ExpandDstUrl(
        self.args[-1], self.gsutil_api)

    # If the destination bucket has versioning enabled iterate with
    # all_versions=True. That way we'll copy all versions if the source bucket
    # is versioned; and by leaving all_versions=False if the destination bucket
    # has versioning disabled we will avoid copying old versions all to the same
    # un-versioned destination object.
    all_versions = False
    try:
      bucket = self._GetBucketWithVersioningConfig(exp_dst_url)
      if bucket and bucket.versioning and bucket.versioning.enabled:
        all_versions = True
    except AccessDeniedException:
      # This happens (in the XML API only) if the user doesn't have OWNER access
      # on the bucket (needed to check if versioning is enabled). In this case
      # fall back to copying all versions (which can be inefficient for the
      # reason noted in the comment above). We don't try to warn the user
      # because that would result in false positive warnings (since we can't
      # check if versioning is enabled on the destination bucket).
      #
      # For JSON, we will silently not return versioning if we don't have
      # access.
      all_versions = True

    name_expansion_iterator = NameExpansionIterator(
        self.command_name, self.debug,
        self.logger, self.gsutil_api, url_strs,
        self.recursion_requested or opts_tuple.perform_mv,
        have_existing_dst_container=have_existing_dst_container,
        project_id=self.project_id, all_versions=all_versions)
    self.have_existing_dst_container = have_existing_dst_container
    self.exp_dst_url = exp_dst_url

    # Use a lock to ensure accurate statistics in the face of
    # multi-threading/multi-processing.
    self.stats_lock = CreateLock()

    # Tracks if any copies failed.
    self.copy_failure_count = 0

    # Start the clock.
    start_time = time.time()

    # Tuple of attributes to share/manage across multiple processes in
    # parallel (-m) mode.
    shared_attrs = ('copy_failure_count', 'total_bytes_transferred')

    # Perform copy requests in parallel (-m) mode, if requested, using
    # configured number of parallel processes and threads. Otherwise,
    # perform requests with sequential function calls in current process.
    self.Apply(_CopyFuncWrapper, name_expansion_iterator,
               _CopyExceptionHandler, shared_attrs, fail_on_error=True)
    self.logger.debug(
        'total_bytes_transferred: %d', self.total_bytes_transferred)

    end_time = time.time()
    self.total_elapsed_time = end_time - start_time

    # Sometimes, particularly when running unit tests, the total elapsed time
    # is really small. On Windows, the timer resolution is too small and
    # causes total_elapsed_time to be zero.
    try:
      float(self.total_bytes_transferred) / float(self.total_elapsed_time)
    except ZeroDivisionError:
      self.total_elapsed_time = 0.01

    self.total_bytes_per_second = (float(self.total_bytes_transferred) /
                                   float(self.total_elapsed_time))

    if self.debug == 3:
      # Note that this only counts the actual GET and PUT bytes for the copy
      # - not any transfers for doing wildcard expansion, the initial
      # HEAD/GET request performed to get the object metadata, etc.
      if self.total_bytes_transferred != 0:
        self.logger.info(
            'Total bytes copied=%d, total elapsed time=%5.3f secs (%sps)',
            self.total_bytes_transferred, self.total_elapsed_time,
            MakeHumanReadable(self.total_bytes_per_second))
    if self.copy_failure_count:
      plural_str = ''
      if self.copy_failure_count > 1:
        plural_str = 's'
      raise CommandException('%d file%s/object%s could not be transferred.' % (
          self.copy_failure_count, plural_str, plural_str))

    return 0

  def _ParseOpts(self):
    opts_tuple = CreateOptsTuple()
    opts_tuple.perform_mv = False
    # exclude_symlinks is handled by Command parent class, so place a copy in
    # self.exclude_symlinks.
    opts_tupleexclude_symlinks = False
    self.exclude_symlinks = False
    opts_tuple.no_clobber = False
    # continue_on_error is handled by Command parent class, so place a copy in
    # self.continue_on_error.
    opts_tuple.continue_on_error = False
    self.continue_on_error = False
    opts_tuple.daisy_chain = False
    opts_tuple.read_args_from_stdin = False
    opts_tuple.print_ver = False
    opts_tuple.use_manifest = False
    opts_tuple.preserve_acl = False
    opts_tuple.canned_acl = None
    # canned, def_acl, and acl_arg are handled by a helper function in parent
    # Command class, so save in Command state rather than opts_tuple.
    self.canned = None
    self.def_acl = None
    self.acl_arg = None

    # self.recursion_requested initialized in command.py (so can be checked
    # in parent class for all commands).
    self.manifest = None
    if self.sub_opts:
      for o, a in self.sub_opts:
        if o == '-a':
          opts_tuple.canned_acl = a
          self.canned = True
          self.def_acl = False
          self.acl_arg = a
        if o == '-c':
          self.continue_on_error = True
        elif o == '-D':
          opts_tuple.daisy_chain = True
        elif o == '-e':
          self.exclude_symlinks = True
        elif o == '-I':
          opts_tuple.read_args_from_stdin = True
        elif o == '-L':
          opts_tuple.use_manifest = True
          self.manifest = Manifest(a)
        elif o == '-M':
          # Note that we signal to the cp command to perform a move (copy
          # followed by remove) and use directory-move naming rules by passing
          # the undocumented (for internal use) -M option when running the cp
          # command from mv.py.
          opts_tuple.perform_mv = True
        elif o == '-n':
          opts_tuple.no_clobber = True
        if o == '-p':
          opts_tuple.preserve_acl = True
        elif o == '-q':
          self.logger.warning(
              'Warning: gsutil cp -q is deprecated, and will be removed in the '
              'future.\nPlease use gsutil -q cp ... instead.')
          self.logger.setLevel(level=logging.WARNING)
        elif o == '-r' or o == '-R':
          self.recursion_requested = True
        elif o == '-v':
          opts_tuple.print_ver = True
    if opts_tuple.preserve_acl and opts_tuple.canned_acl:
      raise CommandException(
          'Specifying both the -p and -a options together is invalid.')
    return opts_tuple

  def _GetBucketWithVersioningConfig(self, exp_dst_url):
    """Gets versioning config for a bucket and ensures that it exists.

    Args:
      exp_dst_url: Wildcard-expanded destination StorageUrl.

    Raises:
      AccessDeniedException: if there was a permissions problem accessing the
                             bucket or its versioning config.
      CommandException: if URL refers to a cloud bucket that does not exist.

    Returns:
      apitools Bucket with versioning configuration.
    """
    bucket = None
    if exp_dst_url.IsCloudUrl() and exp_dst_url.IsBucket():
      try:
        bucket = self.gsutil_api.GetBucket(
            exp_dst_url.bucket_name, provider=exp_dst_url.scheme,
            fields=['versioning'])
      except AccessDeniedException, e:
        raise
      except NotFoundException, e:
        raise CommandException('Destination bucket %s does not exist.' %
                               exp_dst_url.GetUrlString())
      except Exception, e:
        raise CommandException('Error retrieving destination bucket %s: %s' %
                               (exp_dst_url.GetUrlString(), e.message))
      return bucket
