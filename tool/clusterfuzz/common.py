"""Classes & methods to be shared between all commands."""
# Copyright 2016 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import os
import sys
import stat
import subprocess
import logging
import time
import signal
import shutil
import tempfile

import namedlist
import requests_cache
from requests.packages.urllib3.util import retry
from requests import adapters
from requests import exceptions
from backports.shutil_get_terminal_size import get_terminal_size

from cmd_editor import editor
from clusterfuzz import local_logging
from clusterfuzz import output_transformer
from error import error

RETRY_COUNT = 5
RETRY_SLEEP_TIME = 3

BASH_BOLD_MARKER = '\033[1m'
BASH_RESET_STYLE_MARKER = '\033[22m'

BASH_BLUE_MARKER = '\033[36m'
BASH_GREEN_MARKER = '\033[32m'
BASH_YELLOW_MARKER = '\033[33m'
BASH_MAGENTA_MARKER = '\033[35m'
BASH_RESET_COLOR_MARKER = '\033[39m'

NO_SUCH_PROCESS_ERRNO = 3
DEFAULT_READ_BUFFER_LENGTH = 10

CLUSTERFUZZ_DIR = os.path.expanduser(os.path.join('~', '.clusterfuzz'))
CLUSTERFUZZ_CACHE_DIR = os.path.join(CLUSTERFUZZ_DIR, 'cache')
# Because /tmp is too small on Goobuntu.
CLUSTERFUZZ_TMP_DIR = os.path.join(CLUSTERFUZZ_DIR, 'cache', 'tmp')
CLUSTERFUZZ_TESTCASES_DIR = os.path.join(CLUSTERFUZZ_CACHE_DIR, 'testcases')
CLUSTERFUZZ_BUILDS_DIR = os.path.join(CLUSTERFUZZ_CACHE_DIR, 'builds')
AUTH_HEADER_FILE = os.path.join(CLUSTERFUZZ_CACHE_DIR, 'auth_header')
DOMAIN_NAME = 'clusterfuzz.com'
TERMINAL_WIDTH = get_terminal_size().columns
HTTP_CACHE_TTL = 2 * 60
# See: https://github.com/google/clusterfuzz-tools/issues/433
BLACKLISTED_ENVS = {
    'ASAN_OPTIONS': '',
    'CFI_OPTIONS': '',
    'LSAN_OPTIONS': '',
    'MSAN_OPTIONS': '',
    'TSAN_OPTIONS': '',
    'UBSAN_OPTIONS': '',
}
IMPORTANT_DIRS = [
    CLUSTERFUZZ_DIR,
    CLUSTERFUZZ_TMP_DIR,
    CLUSTERFUZZ_BUILDS_DIR,
    CLUSTERFUZZ_TESTCASES_DIR,
    CLUSTERFUZZ_CACHE_DIR
]
logger = logging.getLogger('clusterfuzz')


Options = namedlist.namedlist(
    'Options',
    ['testcase_id', 'current', 'build', 'disable_goma', 'goma_threads',
     'goma_load', 'iterations', 'disable_xvfb', 'target_args', 'edit_mode',
     'skip_deps', 'enable_debug', 'extra_log_params']
)


MEMOIZED_CACHE = {}


def ensure_important_dirs():
  """Ensures important dirs."""
  delete_if_exists(CLUSTERFUZZ_TMP_DIR)
  for path in IMPORTANT_DIRS:
    ensure_dir(path)


def memoize(func):
  """A decorator for caching the method's result using args. There
    are several properties that needs certain actions (e.g. asking for input,
    downloading file). Without memoize, we would need to maintain an explicit
    variable to achieve it.

    This works with both instance methods and module methods."""

  @functools.wraps(func)
  def wrapper(*args, **kwargs):
    """Wrapper function."""
    key = (func, args, tuple(sorted(kwargs.items())))
    if key in MEMOIZED_CACHE:
      return MEMOIZED_CACHE[key]

    result = func(*args, **kwargs)
    MEMOIZED_CACHE[key] = result
    return result
  return wrapper


def get_http():
  """Get the http object."""
  ensure_dir(CLUSTERFUZZ_TESTCASES_DIR)
  http = requests_cache.CachedSession(
      cache_name=os.path.join(CLUSTERFUZZ_TESTCASES_DIR, 'http_cache'),
      backend='sqlite',
      allowable_methods=('GET', 'POST'),
      allowable_codes=[200],
      expire_after=HTTP_CACHE_TTL)
  http.mount(
      'https://',
      adapters.HTTPAdapter(
          # backoff_factor is 0.5. Therefore, the max wait time is 16s.
          retry.Retry(
              total=5, backoff_factor=0.5,
              status_forcelist=[500, 502, 503, 504]))
  )
  return http


def post(url, **kwargs):
  """Make a post request."""
  for i in range(RETRY_COUNT + 1):
    try:
      return get_http().post(url=url, **kwargs)
    except exceptions.ConnectionError:
      if i == RETRY_COUNT:
        raise
      else:
        logger.warn(
            'Failed to reach %s: ConnectionError. Retrying %d/%d.',
            url, i + 1, RETRY_COUNT)
        time.sleep(RETRY_SLEEP_TIME)


class CrashSignature(object):
  """Represents a crash signature (including output)."""

  def __init__(self, crash_type, crash_state_lines, output=''):
    self.crash_type = crash_type
    self.crash_state_lines = tuple(crash_state_lines)
    self.output = output

  def __hash__(self):
    return (self.crash_type, self.crash_state_lines, self.output).__hash__()

  def __eq__(self, other):
    return (isinstance(other, CrashSignature) and
            self.crash_type == other.crash_type and
            self.crash_state_lines == other.crash_state_lines and
            self.output == other.output)


def get_os_name():
  """We need this method because we cannot mock os.name."""
  return os.name


def emphasize(s):
  """Make the text bolder in the logs."""
  return style(s, BASH_BOLD_MARKER, BASH_RESET_STYLE_MARKER)


def colorize(s, color):
  """Add color to the text in the logs."""
  return style(s, color, BASH_RESET_COLOR_MARKER)


def style(s, marker, reset_char):
  """Wrap the string with bash-compatible style."""
  if get_os_name() == 'posix':
    return marker + s + reset_char
  else:
    return s


def get_version():
  """Print version."""
  with open(get_resource(0640, 'resources', 'VERSION')) as f:
    return f.read().strip()


class Definition(object):
  """Holds all the necessary information to initialize a job's builder."""

  def __init__(self, builder, source_name, reproducer, binary_name,
               sanitizer, targets, require_user_data_dir, revision_url):
    if not sanitizer:
      raise error.SanitizerNotProvidedError()
    self.builder = builder
    self.source_name = source_name
    self.reproducer = reproducer
    self.binary_name = binary_name
    self.sanitizer = sanitizer
    self.targets = targets
    self.require_user_data_dir = require_user_data_dir
    self.revision_url = revision_url


def store_auth_header(auth_header):
  """Stores 'auth_header' locally for future access."""
  ensure_dir(os.path.dirname(AUTH_HEADER_FILE))

  with open(AUTH_HEADER_FILE, 'w') as f:
    f.write(auth_header)
  os.chmod(AUTH_HEADER_FILE, stat.S_IWUSR|stat.S_IRUSR)


def get_stored_auth_header():
  """Checks whether there is a valid auth key stored locally."""
  if not os.path.isfile(AUTH_HEADER_FILE):
    return None

  can_group_access = bool(os.stat(AUTH_HEADER_FILE).st_mode & 0070)
  can_other_access = bool(os.stat(AUTH_HEADER_FILE).st_mode & 0007)

  if can_group_access or can_other_access:
    raise error.PermissionsTooPermissiveError(
        AUTH_HEADER_FILE,
        oct(os.stat(AUTH_HEADER_FILE).st_mode & 0777))

  with open(AUTH_HEADER_FILE, 'r') as f:
    return f.read()


def wait_timeout(proc, timeout):
  """If proc runs longer than <timeout> seconds, kill it."""
  if not timeout:
    return

  try:
    for _ in range(0, timeout * 2):
      time.sleep(0.5)
      if proc.poll():
        break

  finally:
    try:
      kill(proc)
    except:  # pylint: disable=bare-except
      pass


def kill(proc):
  """Kill a process multiple times.
    See: https://github.com/google/clusterfuzz-tools/pull/301"""
  try:
    for sig in [signal.SIGTERM, signal.SIGTERM,
                signal.SIGKILL, signal.SIGKILL]:
      logger.debug('Killing pid=%s with %s', proc.pid, sig)
      # Process leader id is the group id.
      os.killpg(proc.pid, sig)

      # Wait for any shutdown stacktrace to be dumped.
      time.sleep(3)

    raise error.KillProcessFailedError(proc.args, proc.pid)
  except OSError as e:
    if e.errno != NO_SUCH_PROCESS_ERRNO:
      raise


def edit_if_needed(content, prefix, comment, should_edit):
  """Edit content in an editor if should_edit is true."""
  if not should_edit:
    return content

  return editor.edit(content, prefix=prefix, comment=comment)


def find_file(target_filename, parent_dir):
  """Return the full path under parent_dir. In Chrome, the binary is
    inside a sub-directory. But, in Android, the binary is in the parent dir.
    This is also useful for finding the testcase file."""
  for root, _, files in os.walk(parent_dir):
    for filename in files:
      if filename == target_filename:
        return os.path.join(root, filename)

  raise Exception(
      'Cannot find file named %s in directory %s.' %
      (target_filename, parent_dir))


def check_binary(binary, cwd):
  """Check if the binary exists."""
  try:
    return subprocess.check_output(['which', binary], cwd=cwd).strip()
  except subprocess.CalledProcessError:
    raise error.NotInstalledError(binary)


class Stdin(object):
  """Represent different ways of setting Popen's stdin."""

  def get(self):
    """Get the stdin handler for Popen."""
    raise NotImplementedError

  def update_cmd_log(self, cmd):
    """Modify the command to represent the stdin in logs."""
    raise NotImplementedError


class BlockStdin(Stdin):
  """Blocking input as opposed to accepting user's input."""

  def get(self):
    """Return subprocess.PIPE because it'll open a new buffer."""
    return subprocess.PIPE

  def update_cmd_log(self, cmd):
    """Return cmd because blocking input doesn't alter
      the command."""
    return cmd


class UserStdin(Stdin):
  """Accept user's input as stdin."""

  def get(self):
    """Return None because that's how it works."""
    return None

  def update_cmd_log(self, cmd):
    """Return cmd because accepting user's input doesn't alter
      the command."""
    return cmd


class StringStdin(Stdin):
  """Send a string as stdin."""

  def __init__(self, input_str):
    self.input_str = input_str
    self.stdin = tempfile.NamedTemporaryFile(delete=False)
    self.stdin.write(input_str)
    self.stdin.flush()
    self.stdin.seek(0)

  def get(self):
    """Get the file handler for the string."""
    return self.stdin

  def update_cmd_log(self, cmd):
    """Add the input filename to the command."""
    return '%s < %s' % (cmd, self.stdin.name)


def start_execute(
    binary, args, cwd, env=None, print_command=True, stdin=None,
    preexec_fn=os.setsid, redirect_stderr_to_stdout=False):
  """Runs a command, and returns the subprocess.Popen object."""
  check_binary(binary, cwd)

  command = (binary + ' ' + args).strip()
  env = env or {}
  stdin = stdin or UserStdin()

  # See https://github.com/google/clusterfuzz-tools/issues/199 why we need this.
  sanitized_env = {}
  for k, v in env.iteritems():
    if v is not None:
      sanitized_env[str(k)] = str(v)

  env_str = ' '.join(
      ['%s="%s"' % (k, v) for k, v in sanitized_env.iteritems()])

  log = colorize(
      stdin.update_cmd_log(
          'Running: %s' % ' '.join([env_str, emphasize(binary), args]).strip()),
      BASH_BLUE_MARKER)
  if print_command:
    logger.info(log)
  else:
    logger.debug(log)

  final_env = os.environ.copy()
  # See: https://github.com/google/clusterfuzz-tools/issues/433
  final_env.update(BLACKLISTED_ENVS)
  final_env.update(sanitized_env)

  proc = subprocess.Popen(
      command,
      shell=True,
      stdin=stdin.get(),
      stdout=subprocess.PIPE,
      stderr=(
          subprocess.STDOUT if redirect_stderr_to_stdout else subprocess.PIPE),
      cwd=cwd,
      env=final_env,
      preexec_fn=preexec_fn)

  setattr(proc, 'args', command)
  return proc


def wait_execute(proc, exit_on_error, capture_output=True, print_output=True,
                 timeout=None, stdout_transformer=None,
                 stderr_transformer=None,
                 read_buffer_length=DEFAULT_READ_BUFFER_LENGTH):
  """Looks after a command as it runs, and prints/returns its output after."""
  if stdout_transformer is None:
    stdout_transformer = output_transformer.Hidden()

  if stderr_transformer is None:
    stderr_transformer = output_transformer.Identity()

  logger.debug('---------------------------------------')
  wait_timeout(proc, timeout)

  output_chunks = []
  stdout_transformer.set_output(sys.stdout)
  stderr_transformer.set_output(sys.stderr)

  # Stdout is printed as the process runs because some commands (e.g. ninja)
  # might take a long time to run.
  for chunk in iter(lambda: proc.stdout.read(read_buffer_length), b''):
    if print_output:
      local_logging.send_output(chunk)
      stdout_transformer.process(chunk)
    if capture_output:
      # According to: http://stackoverflow.com/questions/19926089, this is the
      # fastest way to build strings.
      output_chunks.append(chunk)

  # We cannot read from stderr because it might cause a hang.
  # Therefore, we use communicate() to get stderr instead.
  # See: https://github.com/google/clusterfuzz-tools/issues/278
  stdout_data, stderr_data = proc.communicate()
  stdout_data = stdout_data or ''
  stderr_data = stderr_data or ''
  kill(proc)

  for (transformer, data) in [(stdout_transformer, stdout_data),
                              (stderr_transformer, stderr_data)]:
    if capture_output:
      output_chunks.append(data)

    if print_output:
      local_logging.send_output(data)
      transformer.process(data)
      transformer.flush()

  logger.debug('---------------------------------------')
  if proc.returncode != 0:
    logger.debug('| Return code is non-zero (%d).', proc.returncode)
    if exit_on_error:
      logger.debug('| Exit.')
      raise error.CommandFailedError(proc.args, proc.returncode, stderr_data)
  return proc.returncode, ''.join(output_chunks)


def execute(binary, args, cwd, print_command=True, print_output=True,
            capture_output=True, exit_on_error=True, env=None,
            stdout_transformer=None, stderr_transformer=None, timeout=None,
            stdin=None, preexec_fn=os.setsid,
            redirect_stderr_to_stdout=False,
            read_buffer_length=DEFAULT_READ_BUFFER_LENGTH):
  """Execute a bash command."""
  proc = start_execute(
      binary, args, cwd, env=env, print_command=print_command,
      stdin=stdin, preexec_fn=preexec_fn,
      redirect_stderr_to_stdout=redirect_stderr_to_stdout)
  return wait_execute(
      proc=proc, exit_on_error=exit_on_error, capture_output=capture_output,
      print_output=print_output, timeout=timeout,
      stdout_transformer=stdout_transformer,
      stderr_transformer=stderr_transformer,
      read_buffer_length=read_buffer_length)


def check_confirm(question):
  """Exits the program if the answer is negative, does nothing otherwise."""
  if not confirm(question):
    raise error.UserRespondingNoError(question)


def confirm(question, default='y'):
  """Asks the user a question and returns their answer.
  default can either be 'y', 'n', or None. Answer
  is returned as either True or False."""

  if os.environ.get('CF_QUIET'):
    return True

  accepts = ['y', 'n']
  defaults = '[y/n]'
  if default:
    accepts += ['']
    defaults = defaults.replace(default, default.upper())

  answer = raw_input(emphasize(colorize(
      '%s %s: ' % (question, defaults), BASH_MAGENTA_MARKER))).lower().strip()
  while not answer in accepts:
    answer = raw_input(
        emphasize(colorize(
            'Please type either "y" or "n": ', BASH_MAGENTA_MARKER))
    ).lower().strip()

  if answer == 'y' or (answer == '' and default == 'y'):
    return True
  return False


def ask(question, error_message, validate_fn):
  """Asks question, and keeps asking error_message until validate_fn is True"""

  answer = ''
  while not validate_fn(answer):
    answer = raw_input(emphasize(colorize(
        '%s: ' % question, BASH_MAGENTA_MARKER)))
    question = error_message
  return answer


def get_resource(chmod_permission, *paths):
  """Take a relative filepath and return the actual path. chmod_permission is
    needed because our packaging might destroy the permission."""
  full_path = os.path.join(os.path.dirname(__file__), *paths)
  os.chmod(full_path, chmod_permission)
  return full_path


def delete_if_exists(path):
  """Deletes file if path exists."""
  if os.path.exists(path):
    if os.path.isfile(path):
      os.remove(path)
    else:
      shutil.rmtree(path)


def ensure_dir(path):
  """Make dir if not exist."""
  if os.path.exists(path):
    return

  os.makedirs(path)


def get_valid_abs_dir(path):
  """Return true if path is a valid dir."""
  if not path:
    return None

  abs_path = os.path.abspath(os.path.expanduser(path))

  if not os.path.isdir(abs_path):
    return None

  return abs_path


def gsutil(*args, **kwargs):
  """Run gsutil and raise an elaborated exception if gsutil doesn't exist."""
  try:
    return execute('gsutil', *args, **kwargs)
  except error.NotInstalledError:
    raise error.GsutilNotInstalledError()
