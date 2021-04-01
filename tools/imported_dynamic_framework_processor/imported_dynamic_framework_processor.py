# Copyright 2020 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import shutil
import sys
import time

from build_bazel_rules_apple.tools.bitcode_strip import bitcode_strip
from build_bazel_rules_apple.tools.codesigningtool import codesigningtool
from build_bazel_rules_apple.tools.wrapper_common import lipo


def _zip_framework(framework_temp_path, output_zip_path):
  """Saves the framework as a zip file for caching."""
  zip_epoch_timestamp = 946684800  # 2000-01-01 00:00
  timestamp = zip_epoch_timestamp + time.timezone
  if os.path.exists(framework_temp_path):
    # Apply the fixed utime to the files within directories, then their parent
    # directories and files adjacent to those directories.
    #
    # Avoids accidentally resetting utime on the directories when utime is set
    # on the files within.
    for root, dirs, files in os.walk(framework_temp_path, topdown=False):
      for file_name in dirs + files:
        file_path = os.path.join(root, file_name)
        os.utime(file_path, (timestamp, timestamp))
    os.utime(framework_temp_path, (timestamp, timestamp))
  shutil.make_archive(os.path.splitext(output_zip_path)[0], "zip",
                      os.path.dirname(framework_temp_path),
                      os.path.basename(framework_temp_path))


def _relpath_from_framework(framework_absolute_path):
  """
  Returns a tuple of relative path to the root of the framework bundle,
  and the path to the framework bundle.
  """
  framework_dir = None
  parent_dir = os.path.dirname(framework_absolute_path)
  while parent_dir != "/" and framework_dir is None:
    if parent_dir.endswith(".framework"):
      framework_dir = parent_dir
    else:
      parent_dir = os.path.dirname(parent_dir)

  if parent_dir == "/":
    print("Internal Error: Could not find path in framework: " +
          framework_absolute_path)
    return None

  return os.path.relpath(framework_absolute_path, framework_dir), framework_dir

def _copy_framework_contents(framework_binaries, output_path, slices_needed, strip_bitcode):
  """Copies framework contents to a given path, marking as writable and executable as needed."""
  if not framework_binaries or not output_path:
    return 1
  path_from_framework, framework_dir = _relpath_from_framework(framework_binaries[0])

  # Verify that all binaries, if there are more than one, have the same root.
  for framework_binary in framework_binaries[1:]:
    _, root = _relpath_from_framework(framework_binary)
    if root != framework_dir:
        print("Internal Error: Binary at path {} does not have expected framework root {}".format(framework_binary, framework_dir))
        return 1
  
  # Copy the framework directory to the output path, preserving all symliinks
  if os.path.exists(output_path):
    shutil.rmtree(output_path)
  shutil.copytree(framework_dir, output_path)

  # Set executable permissions on the binaries and strip archs/bitcode if necessary.
  for framework_binary in framework_binaries:
    input_relpath, _ = _relpath_from_framework(framework_binary)
    output_binary = os.path.join(output_path, input_relpath)
    os.chmod(output_binary, 0o755)
    if slices_needed:
      lipo.invoke_lipo(output_binary, slices_needed, output_binary)
    if strip_bitcode:
      bitcode_strip.invoke(output_binary, output_binary)
  
  return 0


def main():
  parser = codesigningtool.generate_arg_parser()
  parser.add_argument(
      "--framework_binary", type=str, required=True, action="append",
      help="path to a binary file scoped to one of the imported frameworks"
  )
  parser.add_argument(
      "--slice", type=str, required=True, action="append", help="binary slice "
      "expected to represent the target architectures"
  )
  parser.add_argument(
      "--strip_bitcode", action="store_true", default=False, help="strip "
      "bitcode from the imported frameworks."
  )
  parser.add_argument(
      "--framework_file", type=str, action="append", help="path to a file "
      "scoped to one of the imported frameworks, distinct from the binary files"
  )
  parser.add_argument(
      "--temp_path", type=str, required=True, help="temporary path to copy "
      "all framework files to"
  )
  parser.add_argument(
      "--output_zip", type=str, required=True, help="path to save the zip file "
      "containing a codesigned, lipoed version of the imported framework"
  )
  args = parser.parse_args()

  all_binary_archs = [arch.replace("sim_", "") for arch in args.slice]
  framework_archs = lipo.find_archs_for_binaries(args.framework_binary)

  if not framework_archs:
    return 1

  # If the imported framework is single architecture, and therefore assumed
  # that it doesn't need to be lipoed, or if the binary architectures match
  # the framework architectures perfectly, treat as a copy instead of a lipo
  # operation.
  if len(framework_archs) == 1 or all_binary_archs == framework_archs:
    slices_needed = []
  else:
    slices_needed = framework_archs.intersection(all_binary_archs)
    if not slices_needed:
      print("Error: Precompiled framework does not share any binary "
            "architectures with the binaries that were built.")
      return 1

  # Delete any existing stale framework files, if any exist.
  if os.path.exists(args.temp_path):
    shutil.rmtree(args.temp_path)
  if os.path.exists(args.output_zip):
    os.remove(args.output_zip)

  status_code = _copy_framework_contents(args.framework_binary, args.temp_path, slices_needed, args.strip_bitcode)
  if status_code:
    return 1

  # Attempt to sign the framework, check for an error when signing.
  status_code = codesigningtool.main(args)
  if status_code:
    return status_code

  _zip_framework(args.temp_path, args.output_zip)


if __name__ == "__main__":
  sys.exit(main())
