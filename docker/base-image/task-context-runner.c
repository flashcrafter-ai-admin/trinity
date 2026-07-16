#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <grp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/prctl.h>
#include <unistd.h>

#define CONTEXT_ROOT "/run/trinity-task-context"
#define MAX_GRANT_BYTES 32768
#define CONTEXT_VERIFIER "/opt/flashcrafter/bin/fc-operation-broker"
#define CLAUDE_RUNTIME "/usr/local/bin/claude"
#define CODEX_RUNTIME "/usr/local/bin/codex"
#define CODEX_SOURCE_PARENT "/home/developer"
#define CODEX_SOURCE_DIRECTORY ".codex-subscription"
#define CODEX_SOURCE_HOME "/home/developer/.codex-subscription"
#define CODEX_TASK_ROOT "/run/trinity-codex"
#define CODEX_RULES_SOURCE "/etc/trinity/codex-operation.rules"
#define CODEX_AUTH_VALIDATOR "/etc/trinity/validate-codex-auth.py"
#define PYTHON_RUNTIME "/usr/bin/python3"
#define MAX_CODEX_AUTH_BYTES 262144
#define CODEX_TASK_PATH_BYTES 192
#define OPERATION_UID 1001
#define OPERATION_GID 1001
#define DEVELOPER_UID 1000
#define DEVELOPER_GID 1000
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
      value.st_uid != 0 || value.st_gid != 0 || (value.st_mode & 07777) != mode) {
    fputs("invalid task context directory boundary\n", stderr);
    _exit(126);
  }
}

static void require_owned_regular(int fd, uid_t uid, gid_t gid, mode_t mode) {
  struct stat value;
  if (fstat(fd, &value) != 0) fail("fstat protected file");
  if (!S_ISREG(value.st_mode) || value.st_uid != uid || value.st_gid != gid ||
      (value.st_mode & 0777) != mode || value.st_nlink != 1) {
    fputs("invalid protected file boundary\n", stderr);
    _exit(126);
  }
}

static void require_owned_directory_fd(int fd, uid_t uid, gid_t gid, mode_t mode) {
  struct stat value;
  if (fstat(fd, &value) != 0) fail("fstat protected directory");
  if (!S_ISDIR(value.st_mode) || value.st_uid != uid || value.st_gid != gid ||
      (value.st_mode & 07777) != mode) {
    fputs("invalid protected directory boundary\n", stderr);
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

static void validate_codex_auth(const char *buffer, size_t length) {
  int input[2];
  if (pipe2(input, O_CLOEXEC) != 0) fail("pipe Codex auth validator");
  pid_t validator = fork();
  if (validator < 0) fail("fork Codex auth validator");
  if (validator == 0) {
    close(input[1]);
    if (dup2(input[0], STDIN_FILENO) < 0) fail("Codex auth validator stdin");
    close(input[0]);
    if (clearenv() != 0 || setenv("PATH", "/usr/bin:/bin", 1) != 0 ||
        setenv("HOME", "/var/empty", 1) != 0 || setenv("LANG", "C.UTF-8", 1) != 0)
      fail("Codex auth validator environment");
    execl(PYTHON_RUNTIME, "python3", "-I", "-S", "-B", CODEX_AUTH_VALIDATOR,
          (char *)NULL);
    fail("exec Codex auth validator");
  }
  close(input[0]);
  write_all(input[1], buffer, length);
  if (close(input[1]) != 0) fail("close Codex auth validator input");
  int status = 0;
  while (waitpid(validator, &status, 0) < 0) {
    if (errno != EINTR) fail("wait Codex auth validator");
  }
  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
    fputs("Codex subscription auth failed the sealed runtime policy\n", stderr);
    _exit(126);
  }
}

static char *read_protected_file(int fd, uid_t uid, gid_t gid, mode_t mode,
                                 size_t maximum, size_t *length_out) {
  struct stat before;
  if (fstat(fd, &before) != 0) fail("fstat protected input");
  require_owned_regular(fd, uid, gid, mode);
  if (before.st_size <= 0 || (unsigned long long)before.st_size > maximum) {
    fputs("protected input size is invalid\n", stderr);
    _exit(126);
  }
  size_t length = (size_t)before.st_size;
  char *buffer = malloc(length);
  if (buffer == NULL) fail("malloc protected input");
  if (lseek(fd, 0, SEEK_SET) < 0) fail("seek protected input");
  size_t offset = 0;
  while (offset < length) {
    ssize_t count = read(fd, buffer + offset, length - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) fail("read protected input");
    offset += (size_t)count;
  }
  struct stat after;
  if (fstat(fd, &after) != 0 || before.st_dev != after.st_dev ||
      before.st_ino != after.st_ino || before.st_size != after.st_size ||
      before.st_mtim.tv_sec != after.st_mtim.tv_sec ||
      before.st_mtim.tv_nsec != after.st_mtim.tv_nsec ||
      before.st_ctim.tv_sec != after.st_ctim.tv_sec ||
      before.st_ctim.tv_nsec != after.st_ctim.tv_nsec) {
    memset(buffer, 0, length);
    free(buffer);
    fputs("protected input changed while its descriptor was read\n", stderr);
    _exit(126);
  }
  *length_out = length;
  return buffer;
}

static int same_file_identity(const struct stat *expected, const struct stat *actual) {
  return expected->st_dev == actual->st_dev && expected->st_ino == actual->st_ino &&
         expected->st_uid == actual->st_uid && expected->st_gid == actual->st_gid &&
         expected->st_mode == actual->st_mode && expected->st_nlink == actual->st_nlink &&
         expected->st_size == actual->st_size &&
         expected->st_mtim.tv_sec == actual->st_mtim.tv_sec &&
         expected->st_mtim.tv_nsec == actual->st_mtim.tv_nsec &&
         expected->st_ctim.tv_sec == actual->st_ctim.tv_sec &&
         expected->st_ctim.tv_nsec == actual->st_ctim.tv_nsec;
}

static int open_codex_auth_source(int *parent_fd_out, int *directory_fd_out,
                                  struct stat *directory_identity_out,
                                  struct stat *auth_identity_out) {
  int parent_fd =
      open(CODEX_SOURCE_PARENT, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (parent_fd < 0) fail("open Codex subscription parent");
  int directory_fd = openat(parent_fd, CODEX_SOURCE_DIRECTORY,
                            O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (directory_fd < 0) fail("open Codex subscription directory");
  require_owned_directory_fd(directory_fd, DEVELOPER_UID, DEVELOPER_GID, 0700);
  if (fstat(directory_fd, directory_identity_out) != 0)
    fail("identify Codex subscription directory");
  int fd = openat(directory_fd, "auth.json", O_RDWR | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0) fail("open Codex subscription auth");
  require_owned_regular(fd, DEVELOPER_UID, DEVELOPER_GID, 0600);
  if (flock(fd, LOCK_EX) != 0) fail("lock Codex subscription auth");
  if (fstat(fd, auth_identity_out) != 0) fail("identify Codex subscription auth");
  struct stat named_directory;
  struct stat named_auth;
  if (fstatat(parent_fd, CODEX_SOURCE_DIRECTORY, &named_directory,
              AT_SYMLINK_NOFOLLOW) != 0 ||
      fstatat(directory_fd, "auth.json", &named_auth, AT_SYMLINK_NOFOLLOW) != 0 ||
      !same_file_identity(directory_identity_out, &named_directory) ||
      !same_file_identity(auth_identity_out, &named_auth)) {
    fputs("Codex subscription projection changed while it was opened\n", stderr);
    _exit(126);
  }
  *parent_fd_out = parent_fd;
  *directory_fd_out = directory_fd;
  return fd;
}

static void codex_task_home_path(char *path, size_t size) {
  unsigned char random_bytes[24];
  size_t offset = 0;
  while (offset < sizeof(random_bytes)) {
    ssize_t count = getrandom(random_bytes + offset, sizeof(random_bytes) - offset, 0);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) fail("generate Codex task home identity");
    offset += (size_t)count;
  }
  char token[sizeof(random_bytes) * 2 + 1];
  for (size_t index = 0; index < sizeof(random_bytes); index++)
    snprintf(token + index * 2, 3, "%02x", random_bytes[index]);
  memset(random_bytes, 0, sizeof(random_bytes));
  if (snprintf(path, size, CODEX_TASK_ROOT "/%s", token) >= (int)size)
    fail("Codex task home path");
  memset(token, 0, sizeof(token));
}

static void copy_protected_file(int source_fd, uid_t source_uid, gid_t source_gid,
                                mode_t source_mode, int destination_fd,
                                uid_t destination_uid, gid_t destination_gid,
                                mode_t destination_mode, size_t maximum,
                                int validate_auth) {
  size_t length = 0;
  char *buffer = read_protected_file(
      source_fd, source_uid, source_gid, source_mode, maximum, &length);
  if (validate_auth) validate_codex_auth(buffer, length);
  if (fchown(destination_fd, destination_uid, destination_gid) != 0 ||
      fchmod(destination_fd, destination_mode) != 0)
    fail("protect copied file");
  write_all(destination_fd, buffer, length);
  memset(buffer, 0, length);
  free(buffer);
  if (fsync(destination_fd) != 0) fail("persist copied file");
  require_owned_regular(
      destination_fd, destination_uid, destination_gid, destination_mode);
}

static void install_codex_task_home(const char *path, int auth_source_fd) {
  if (mkdir(CODEX_TASK_ROOT, 0711) != 0 && errno != EEXIST)
    fail("mkdir Codex task root");
  require_root_owned_directory(CODEX_TASK_ROOT, 0711);

  if (mkdir(path, 01777) != 0) fail("mkdir Codex task home");
  if (chown(path, 0, 0) != 0 || chmod(path, 01777) != 0)
    fail("protect Codex task home");
  require_root_owned_directory(path, 01777);
  int directory_fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (directory_fd < 0) fail("open Codex task home");

  int auth_fd = openat(directory_fd, "auth.json",
                       O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (auth_fd < 0) fail("create Codex task auth");
  copy_protected_file(auth_source_fd, DEVELOPER_UID, DEVELOPER_GID, 0600,
                      auth_fd, OPERATION_UID, OPERATION_GID, 0600,
                      MAX_CODEX_AUTH_BYTES, 1);
  if (close(auth_fd) != 0) fail("close Codex task auth");

  if (mkdirat(directory_fd, "rules", 0555) != 0) fail("mkdir Codex task rules");
  int rules_directory_fd =
      openat(directory_fd, "rules", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (rules_directory_fd < 0) fail("open Codex task rules");
  int rules_source_fd = open(CODEX_RULES_SOURCE, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (rules_source_fd < 0) fail("open Codex operation rules");
  int rules_fd = openat(rules_directory_fd, "default.rules",
                        O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0444);
  if (rules_fd < 0) fail("create Codex task rules");
  copy_protected_file(rules_source_fd, 0, 0, 0444, rules_fd, 0, 0, 0444, 65536, 0);
  if (close(rules_fd) != 0 || close(rules_source_fd) != 0)
    fail("close Codex task rules");
  if (fsync(rules_directory_fd) != 0 || close(rules_directory_fd) != 0)
    fail("persist Codex task rules");
  if (fsync(directory_fd) != 0 || close(directory_fd) != 0)
    fail("persist Codex task home");
}

static int source_projection_unchanged(int parent_fd, int directory_fd, int auth_fd,
                                       const struct stat *directory_identity,
                                       const struct stat *auth_identity) {
  struct stat current_directory;
  struct stat current_auth;
  struct stat named_directory;
  struct stat named_auth;
  return fstat(directory_fd, &current_directory) == 0 &&
         fstat(auth_fd, &current_auth) == 0 &&
         fstatat(parent_fd, CODEX_SOURCE_DIRECTORY, &named_directory,
                 AT_SYMLINK_NOFOLLOW) == 0 &&
         fstatat(directory_fd, "auth.json", &named_auth, AT_SYMLINK_NOFOLLOW) == 0 &&
         same_file_identity(directory_identity, &current_directory) &&
         same_file_identity(auth_identity, &current_auth) &&
         current_directory.st_dev == named_directory.st_dev &&
         current_directory.st_ino == named_directory.st_ino &&
         S_ISDIR(named_directory.st_mode) &&
         current_auth.st_dev == named_auth.st_dev && current_auth.st_ino == named_auth.st_ino &&
         S_ISREG(named_auth.st_mode);
}

static int refresh_codex_auth(const char *path, int auth_source_parent_fd,
                              int auth_source_directory_fd, int auth_source_fd,
                              const struct stat *directory_identity,
                              const struct stat *auth_identity) {
  int directory_fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (directory_fd < 0) return -1;
  int auth_fd = openat(directory_fd, "auth.json", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (auth_fd < 0) {
    close(directory_fd);
    return -1;
  }
  if (!source_projection_unchanged(auth_source_parent_fd, auth_source_directory_fd,
                                   auth_source_fd,
                                   directory_identity, auth_identity)) {
    close(auth_fd);
    close(directory_fd);
    return -1;
  }
  size_t length = 0;
  char *buffer = read_protected_file(
      auth_fd, OPERATION_UID, OPERATION_GID, 0600, MAX_CODEX_AUTH_BYTES, &length);
  validate_codex_auth(buffer, length);
  int result = 0;
  if (!source_projection_unchanged(auth_source_parent_fd, auth_source_directory_fd,
                                   auth_source_fd,
                                   directory_identity, auth_identity) ||
      lseek(auth_source_fd, 0, SEEK_SET) < 0 ||
      ftruncate(auth_source_fd, 0) != 0) {
    result = -1;
  } else {
    write_all(auth_source_fd, buffer, length);
    if (fsync(auth_source_fd) != 0) result = -1;
    struct stat named_auth;
    struct stat current_auth;
    if (fstat(auth_source_fd, &current_auth) != 0 ||
        fstatat(auth_source_directory_fd, "auth.json", &named_auth, AT_SYMLINK_NOFOLLOW) != 0 ||
        current_auth.st_dev != named_auth.st_dev || current_auth.st_ino != named_auth.st_ino ||
        !S_ISREG(named_auth.st_mode) || named_auth.st_uid != DEVELOPER_UID ||
        named_auth.st_gid != DEVELOPER_GID || (named_auth.st_mode & 0777) != 0600 ||
        named_auth.st_nlink != 1)
      result = -1;
  }
  memset(buffer, 0, length);
  free(buffer);
  if (close(auth_fd) != 0 || close(directory_fd) != 0) result = -1;
  return result;
}

static int remove_tree_node(const char *path, const struct stat *value,
                            int kind, struct FTW *context) {
  (void)value;
  (void)kind;
  (void)context;
  return remove(path);
}

static void remove_codex_task_home(const char *path) {
  require_root_owned_directory(path, 01777);
  if (nftw(path, remove_tree_node, 32, FTW_DEPTH | FTW_PHYS | FTW_MOUNT) != 0)
    fail("remove Codex task home");
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

static void drop_to_operation_identity(void) {
  if (setgroups(0, NULL) != 0) fail("setgroups");
  if (setresgid(OPERATION_GID, OPERATION_GID, OPERATION_GID) != 0) fail("setresgid");
  if (setresuid(OPERATION_UID, OPERATION_UID, OPERATION_UID) != 0) fail("setresuid");
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
    drop_to_operation_identity();
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
  int is_claude = argc >= 2 && strcmp(argv[1], CLAUDE_RUNTIME) == 0;
  int is_codex = argc >= 2 && strcmp(argv[1], CODEX_RUNTIME) == 0;
  if (argc < 2 || (!is_claude && !is_codex) || getuid() != 1000 || geteuid() != 0) {
    fputs("invalid trusted task context invocation\n", stderr);
    return 126;
  }

  char grant[MAX_GRANT_BYTES + 1];
  size_t grant_length = read_grant(grant);
  verify_grant(grant, grant_length);

  char *claude_oauth = is_claude ? copy_optional_environment("CLAUDE_CODE_OAUTH_TOKEN") : NULL;
  char *anthropic_api =
      is_claude && claude_oauth == NULL ? copy_optional_environment("ANTHROPIC_API_KEY") : NULL;
  char *anthropic_auth =
      is_claude && claude_oauth == NULL && anthropic_api == NULL
          ? copy_optional_environment("ANTHROPIC_AUTH_TOKEN")
          : NULL;
  char *codex_home = is_codex ? copy_optional_environment("CODEX_HOME") : NULL;
  if (is_codex && (codex_home == NULL || strcmp(codex_home, CODEX_SOURCE_HOME) != 0)) {
    fputs("sealed Codex home is invalid\n", stderr);
    _exit(126);
  }
  int codex_auth_source_directory_fd = -1;
  int codex_auth_source_parent_fd = -1;
  struct stat codex_auth_source_directory_identity = {0};
  struct stat codex_auth_source_identity = {0};
  int codex_auth_source_fd =
      is_codex ? open_codex_auth_source(&codex_auth_source_parent_fd,
                                        &codex_auth_source_directory_fd,
                                        &codex_auth_source_directory_identity,
                                        &codex_auth_source_identity)
               : -1;
  char codex_task_home[CODEX_TASK_PATH_BYTES] = {0};
  if (is_codex) codex_task_home_path(codex_task_home, sizeof(codex_task_home));
  char *execution_tag = copy_optional_environment("TRINITY_EXECUTION_ID");
  char *http_proxy = is_claude ? copy_optional_environment("HTTP_PROXY") : NULL;
  char *https_proxy = is_claude ? copy_optional_environment("HTTPS_PROXY") : NULL;
  char *no_proxy = is_claude ? copy_optional_environment("NO_PROXY") : NULL;
  char *ssl_cert_file = is_claude ? copy_optional_environment("SSL_CERT_FILE") : NULL;
  char *ssl_cert_dir = is_claude ? copy_optional_environment("SSL_CERT_DIR") : NULL;
  char *node_ca = is_claude ? copy_optional_environment("NODE_EXTRA_CA_CERTS") : NULL;

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
    if (is_codex) {
      close(codex_auth_source_fd);
      close(codex_auth_source_directory_fd);
      close(codex_auth_source_parent_fd);
    }
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
        setenv("SHELL", is_codex ? "/opt/flashcrafter/bin/codex-operation-shell" : "/bin/false", 1) != 0 ||
        setenv("TMPDIR", "/tmp", 1) != 0 ||
        setenv("LANG", "C.UTF-8", 1) != 0 ||
        setenv("PATH", "/opt/flashcrafter/bin:/usr/local/bin:/usr/bin:/bin", 1) != 0)
      fail("setenv sealed runtime");
    set_optional_environment("CLAUDE_CODE_OAUTH_TOKEN", claude_oauth);
    set_optional_environment("ANTHROPIC_API_KEY", anthropic_api);
    set_optional_environment("ANTHROPIC_AUTH_TOKEN", anthropic_auth);
    set_optional_environment("CODEX_HOME", is_codex ? codex_task_home : NULL);
    set_optional_environment("TRINITY_EXECUTION_ID", execution_tag);
    set_optional_environment("HTTP_PROXY", http_proxy);
    set_optional_environment("HTTPS_PROXY", https_proxy);
    set_optional_environment("NO_PROXY", no_proxy);
    set_optional_environment("SSL_CERT_FILE", ssl_cert_file);
    set_optional_environment("SSL_CERT_DIR", ssl_cert_dir);
    set_optional_environment("NODE_EXTRA_CA_CERTS", node_ca);
    wipe_free(codex_home);
    drop_to_operation_identity();
    execv(argv[1], &argv[1]);
    fail("execv");
  }

  wipe_free(claude_oauth);
  wipe_free(anthropic_api);
  wipe_free(anthropic_auth);
  wipe_free(codex_home);
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
  if (is_codex) install_codex_task_home(codex_task_home, codex_auth_source_fd);
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
  int codex_refresh_ok = 1;
  if (is_codex) {
    codex_refresh_ok =
        refresh_codex_auth(codex_task_home, codex_auth_source_parent_fd,
                           codex_auth_source_directory_fd, codex_auth_source_fd,
                           &codex_auth_source_directory_identity,
                           &codex_auth_source_identity) == 0;
    remove_codex_task_home(codex_task_home);
    if (close(codex_auth_source_fd) != 0) codex_refresh_ok = 0;
    if (close(codex_auth_source_directory_fd) != 0) codex_refresh_ok = 0;
    if (close(codex_auth_source_parent_fd) != 0) codex_refresh_ok = 0;
  }
  remove_context(pid);

  if (!codex_refresh_ok) {
    fputs("Codex subscription auth refresh persistence failed\n", stderr);
    return 126;
  }

  if (WIFEXITED(status)) return WEXITSTATUS(status);
  if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
  return 126;
}
