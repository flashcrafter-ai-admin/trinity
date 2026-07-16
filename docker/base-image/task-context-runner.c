#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/prctl.h>
#include <unistd.h>

#define CONTEXT_ROOT "/run/trinity-task-context"
#define MAX_GRANT_BYTES 32768
#define CONTEXT_VERIFIER "/opt/flashcrafter/bin/fc-operation-broker"
#define OPERATION_UID 1001
#define OPERATION_GID 1001
#define OPERATION_HOME "/var/empty"

static volatile sig_atomic_t child_pid = -1;

static void fail(const char *message) {
  perror(message);
  _exit(126);
}

static void forward_signal(int signal_number) {
  if (child_pid > 0) kill((pid_t)child_pid, signal_number);
}

static void require_root_owned_regular(int fd, mode_t mode) {
  struct stat value;
  if (fstat(fd, &value) != 0) fail("fstat");
  if (!S_ISREG(value.st_mode) || value.st_uid != 0 || value.st_gid != 0 ||
      (value.st_mode & 0777) != mode || value.st_nlink != 1) {
    fputs("invalid task context file boundary\n", stderr);
    _exit(126);
  }
}

static void require_root_owned_directory(const char *path, mode_t mode) {
  struct stat value;
  if (lstat(path, &value) != 0 || !S_ISDIR(value.st_mode) || S_ISLNK(value.st_mode) ||
      value.st_uid != 0 || value.st_gid != 0 || (value.st_mode & 0777) != mode) {
    fputs("invalid task context directory boundary\n", stderr);
    _exit(126);
  }
}

static size_t read_grant(char *buffer) {
  size_t length = 0;
  int dots = 0;
  for (;;) {
    char byte;
    ssize_t count = read(STDIN_FILENO, &byte, 1);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0 || byte == '\n') break;
    if (length >= MAX_GRANT_BYTES) {
      fputs("operation grant is too large\n", stderr);
      _exit(126);
    }
    if (!((byte >= 'a' && byte <= 'z') || (byte >= 'A' && byte <= 'Z') ||
          (byte >= '0' && byte <= '9') || byte == '_' || byte == '-' || byte == '.')) {
      fputs("operation grant framing is invalid\n", stderr);
      _exit(126);
    }
    if (byte == '.') dots++;
    buffer[length++] = byte;
  }
  if (length < 64 || dots != 1) {
    fputs("operation grant framing is invalid\n", stderr);
    _exit(126);
  }
  buffer[length] = '\0';
  return length;
}

static void write_all(int fd, const char *buffer, size_t length) {
  size_t offset = 0;
  while (offset < length) {
    ssize_t count = write(fd, buffer + offset, length - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) fail("write");
    offset += (size_t)count;
  }
}

static char *copy_optional_environment(const char *name) {
  const char *value = getenv(name);
  if (value == NULL || value[0] == '\0') return NULL;
  size_t length = strlen(value);
  if (length > 65536) {
    fputs("sealed runtime environment value is too large\n", stderr);
    _exit(126);
  }
  char *copy = malloc(length + 1);
  if (copy == NULL) fail("malloc environment");
  memcpy(copy, value, length + 1);
  return copy;
}

static void set_optional_environment(const char *name, const char *value) {
  if (value != NULL && setenv(name, value, 1) != 0) fail("setenv optional");
}

static void wipe_free(char *value) {
  if (value == NULL) return;
  memset(value, 0, strlen(value));
  free(value);
}

static void verify_grant(const char *grant, size_t length) {
  struct stat verifier;
  if (lstat(CONTEXT_VERIFIER, &verifier) != 0 || !S_ISREG(verifier.st_mode) ||
      S_ISLNK(verifier.st_mode) || verifier.st_uid != 0 || verifier.st_gid != 0 ||
      (verifier.st_mode & 04777) != 04555) {
    fputs("trusted task context verifier is unavailable\n", stderr);
    _exit(126);
  }

  int input[2];
  if (pipe2(input, O_CLOEXEC) != 0) fail("pipe2");
  pid_t verifier_pid = fork();
  if (verifier_pid < 0) fail("fork verifier");
  if (verifier_pid == 0) {
    if (dup2(input[0], STDIN_FILENO) < 0) fail("dup2 verifier");
    close(input[0]);
    close(input[1]);
    execl(CONTEXT_VERIFIER, CONTEXT_VERIFIER, "verify-context", (char *)NULL);
    fail("exec verifier");
  }
  close(input[0]);
  write_all(input[1], grant, length);
  write_all(input[1], "\n", 1);
  close(input[1]);
  int status = 0;
  while (waitpid(verifier_pid, &status, 0) < 0) {
    if (errno != EINTR) fail("wait verifier");
  }
  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
    fputs("operation grant verification failed\n", stderr);
    _exit(126);
  }
}

static unsigned long long process_start_time(pid_t pid) {
  char path[64];
  if (snprintf(path, sizeof(path), "/proc/%ld/stat", (long)pid) >= (int)sizeof(path))
    fail("proc path");
  int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0) fail("open process identity");
  char body[4096];
  ssize_t count = read(fd, body, sizeof(body) - 1);
  close(fd);
  if (count <= 0) fail("read process identity");
  body[count] = '\0';
  char *cursor = strrchr(body, ')');
  if (cursor == NULL || cursor[1] != ' ') fail("parse process identity");
  cursor += 2;
  char *save = NULL;
  char *token = strtok_r(cursor, " ", &save);
  for (int field = 3; token != NULL; field++, token = strtok_r(NULL, " ", &save)) {
    if (field == 22) {
      char *end = NULL;
      unsigned long long value = strtoull(token, &end, 10);
      if (end == token || *end != '\0') fail("parse process start time");
      return value;
    }
  }
  fail("missing process start time");
  return 0;
}

static void clean_stale_context(const char *path) {
  require_root_owned_directory(path, 0700);
  int fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0) fail("open stale context");
  if (unlinkat(fd, "operation-grant", 0) != 0 && errno != ENOENT) fail("unlink stale grant");
  if (unlinkat(fd, "session-start", 0) != 0 && errno != ENOENT) fail("unlink stale identity");
  close(fd);
  if (rmdir(path) != 0) fail("remove stale context");
}

static void install_context(pid_t session_id, const char *grant, size_t length) {
  if (mkdir(CONTEXT_ROOT, 0700) != 0 && errno != EEXIST) fail("mkdir context root");
  require_root_owned_directory(CONTEXT_ROOT, 0700);

  char path[128];
  if (snprintf(path, sizeof(path), CONTEXT_ROOT "/%ld", (long)session_id) >= (int)sizeof(path))
    fail("context path");
  if (mkdir(path, 0700) != 0) {
    if (errno != EEXIST) fail("mkdir task context");
    clean_stale_context(path);
    if (mkdir(path, 0700) != 0) fail("mkdir task context");
  }
  require_root_owned_directory(path, 0700);
  int directory_fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (directory_fd < 0) fail("open task context");

  int grant_fd = openat(directory_fd, "operation-grant",
                        O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (grant_fd < 0) fail("open context grant");
  require_root_owned_regular(grant_fd, 0600);
  write_all(grant_fd, grant, length);
  write_all(grant_fd, "\n", 1);
  if (fsync(grant_fd) != 0 || close(grant_fd) != 0) fail("persist context grant");

  int identity_fd = openat(directory_fd, "session-start",
                           O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (identity_fd < 0) fail("open context identity");
  require_root_owned_regular(identity_fd, 0600);
  char identity[64];
  int identity_length = snprintf(identity, sizeof(identity), "%llu\n", process_start_time(session_id));
  if (identity_length <= 0 || identity_length >= (int)sizeof(identity)) fail("format identity");
  write_all(identity_fd, identity, (size_t)identity_length);
  if (fsync(identity_fd) != 0 || close(identity_fd) != 0) fail("persist context identity");
  if (fsync(directory_fd) != 0) fail("persist task context");
  close(directory_fd);
}

static void remove_context(pid_t session_id) {
  char path[128];
  if (snprintf(path, sizeof(path), CONTEXT_ROOT "/%ld", (long)session_id) >= (int)sizeof(path))
    return;
  struct stat value;
  if (lstat(path, &value) != 0 || !S_ISDIR(value.st_mode) || S_ISLNK(value.st_mode) ||
      value.st_uid != 0 || value.st_gid != 0 || (value.st_mode & 0777) != 0700)
    return;
  int fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0) return;
  unlinkat(fd, "operation-grant", 0);
  unlinkat(fd, "session-start", 0);
  fsync(fd);
  close(fd);
  rmdir(path);
}

int main(int argc, char **argv) {
  if (argc < 2 || strcmp(argv[1], "/usr/local/bin/claude") != 0 || getuid() != 1000 ||
      geteuid() != 0) {
    fputs("invalid trusted task context invocation\n", stderr);
    return 126;
  }

  char grant[MAX_GRANT_BYTES + 1];
  size_t grant_length = read_grant(grant);
  verify_grant(grant, grant_length);

  char *claude_oauth = copy_optional_environment("CLAUDE_CODE_OAUTH_TOKEN");
  char *anthropic_api =
      claude_oauth == NULL ? copy_optional_environment("ANTHROPIC_API_KEY") : NULL;
  char *anthropic_auth =
      claude_oauth == NULL && anthropic_api == NULL
          ? copy_optional_environment("ANTHROPIC_AUTH_TOKEN")
          : NULL;
  char *execution_tag = copy_optional_environment("TRINITY_EXECUTION_ID");
  char *http_proxy = copy_optional_environment("HTTP_PROXY");
  char *https_proxy = copy_optional_environment("HTTPS_PROXY");
  char *no_proxy = copy_optional_environment("NO_PROXY");
  char *ssl_cert_file = copy_optional_environment("SSL_CERT_FILE");
  char *ssl_cert_dir = copy_optional_environment("SSL_CERT_DIR");
  char *node_ca = copy_optional_environment("NODE_EXTRA_CA_CERTS");

  int ready[2];
  int release[2];
  if (pipe2(ready, O_CLOEXEC) != 0 || pipe2(release, O_CLOEXEC) != 0) fail("pipe2 child");
  pid_t pid = fork();
  if (pid < 0) fail("fork");
  if (pid == 0) {
    close(ready[0]);
    close(release[1]);
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() == 1) fail("parent death signal");
    if (setsid() < 0) fail("setsid");
    write_all(ready[1], "R", 1);
    close(ready[1]);
    char go = 0;
    while (read(release[0], &go, 1) < 0 && errno == EINTR) {}
    close(release[0]);
    if (go != 'G') _exit(126);
    signal(SIGINT, SIG_DFL);
    signal(SIGTERM, SIG_DFL);
    signal(SIGHUP, SIG_DFL);
    if (clearenv() != 0) fail("clearenv");
    if (setenv("HOME", OPERATION_HOME, 1) != 0 ||
        setenv("USER", "fc-operation", 1) != 0 ||
        setenv("LOGNAME", "fc-operation", 1) != 0 ||
        setenv("SHELL", "/bin/false", 1) != 0 ||
        setenv("TMPDIR", "/tmp", 1) != 0 ||
        setenv("LANG", "C.UTF-8", 1) != 0 ||
        setenv("PATH", "/opt/flashcrafter/bin:/usr/local/bin:/usr/bin:/bin", 1) != 0)
      fail("setenv sealed runtime");
    set_optional_environment("CLAUDE_CODE_OAUTH_TOKEN", claude_oauth);
    set_optional_environment("ANTHROPIC_API_KEY", anthropic_api);
    set_optional_environment("ANTHROPIC_AUTH_TOKEN", anthropic_auth);
    set_optional_environment("TRINITY_EXECUTION_ID", execution_tag);
    set_optional_environment("HTTP_PROXY", http_proxy);
    set_optional_environment("HTTPS_PROXY", https_proxy);
    set_optional_environment("NO_PROXY", no_proxy);
    set_optional_environment("SSL_CERT_FILE", ssl_cert_file);
    set_optional_environment("SSL_CERT_DIR", ssl_cert_dir);
    set_optional_environment("NODE_EXTRA_CA_CERTS", node_ca);
    if (setgroups(0, NULL) != 0) fail("setgroups");
    if (setresgid(OPERATION_GID, OPERATION_GID, OPERATION_GID) != 0) fail("setresgid");
    if (setresuid(OPERATION_UID, OPERATION_UID, OPERATION_UID) != 0) fail("setresuid");
    execv(argv[1], &argv[1]);
    fail("execv");
  }

  wipe_free(claude_oauth);
  wipe_free(anthropic_api);
  wipe_free(anthropic_auth);
  wipe_free(execution_tag);
  wipe_free(http_proxy);
  wipe_free(https_proxy);
  wipe_free(no_proxy);
  wipe_free(ssl_cert_file);
  wipe_free(ssl_cert_dir);
  wipe_free(node_ca);

  close(ready[1]);
  close(release[0]);
  child_pid = pid;
  char marker = 0;
  while (read(ready[0], &marker, 1) < 0 && errno == EINTR) {}
  close(ready[0]);
  if (marker != 'R' || getsid(pid) != pid) {
    kill(pid, SIGKILL);
    fputs("task session initialization failed\n", stderr);
    return 126;
  }
  install_context(pid, grant, grant_length);
  memset(grant, 0, sizeof(grant));
  write_all(release[1], "G", 1);
  close(release[1]);

  struct sigaction action = {0};
  action.sa_handler = forward_signal;
  sigemptyset(&action.sa_mask);
  sigaction(SIGINT, &action, NULL);
  sigaction(SIGTERM, &action, NULL);
  sigaction(SIGHUP, &action, NULL);

  int status = 0;
  while (waitpid(pid, &status, 0) < 0) {
    if (errno != EINTR) fail("waitpid");
  }
  child_pid = -1;
  remove_context(pid);

  if (WIFEXITED(status)) return WEXITSTATUS(status);
  if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
  return 126;
}
