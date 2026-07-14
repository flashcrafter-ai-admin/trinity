#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define CONTEXT_DIR "/run/trinity-task-context"
#define CONTEXT_FILE CONTEXT_DIR "/operation-grant"
#define LOCK_FILE CONTEXT_DIR "/lock"
#define MAX_GRANT_BYTES 32768
#define CONTEXT_VERIFIER "/opt/flashcrafter/bin/fc-operation-broker"

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

static int install_context(const char *grant, size_t length) {
  if (mkdir(CONTEXT_DIR, 0700) != 0 && errno != EEXIST) fail("mkdir");
  struct stat directory;
  if (lstat(CONTEXT_DIR, &directory) != 0 || !S_ISDIR(directory.st_mode) ||
      S_ISLNK(directory.st_mode) || directory.st_uid != 0 || directory.st_gid != 0 ||
      (directory.st_mode & 0777) != 0700) {
    fputs("invalid task context directory boundary\n", stderr);
    return -1;
  }

  int lock_fd = open(LOCK_FILE, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (lock_fd < 0) fail("open lock");
  if (fchown(lock_fd, 0, 0) != 0 || fchmod(lock_fd, 0600) != 0) fail("secure lock");
  require_root_owned_regular(lock_fd, 0600);
  if (flock(lock_fd, LOCK_EX | LOCK_NB) != 0) {
    fputs("trusted task context is already active\n", stderr);
    close(lock_fd);
    return -1;
  }

  char temporary[256];
  if (snprintf(temporary, sizeof(temporary), CONTEXT_DIR "/operation-grant.%ld", (long)getpid()) >=
      (int)sizeof(temporary)) {
    fputs("task context path is too long\n", stderr);
    close(lock_fd);
    return -1;
  }
  unlink(temporary);
  int context_fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (context_fd < 0) fail("open context");
  if (fchown(context_fd, 0, 0) != 0 || fchmod(context_fd, 0600) != 0) fail("secure context");
  require_root_owned_regular(context_fd, 0600);
  write_all(context_fd, grant, length);
  write_all(context_fd, "\n", 1);
  if (fsync(context_fd) != 0) fail("fsync context");
  if (close(context_fd) != 0) fail("close context");
  if (rename(temporary, CONTEXT_FILE) != 0) fail("activate context");

  int directory_fd = open(CONTEXT_DIR, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
  if (directory_fd < 0 || fsync(directory_fd) != 0) fail("fsync context directory");
  close(directory_fd);
  return lock_fd;
}

static void remove_context(void) {
  struct stat value;
  if (lstat(CONTEXT_FILE, &value) == 0 && S_ISREG(value.st_mode) && !S_ISLNK(value.st_mode) &&
      value.st_uid == 0 && value.st_gid == 0) {
    unlink(CONTEXT_FILE);
  }
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
  int lock_fd = install_context(grant, grant_length);
  memset(grant, 0, sizeof(grant));
  if (lock_fd < 0) return 75;

  struct sigaction action = {0};
  action.sa_handler = forward_signal;
  sigemptyset(&action.sa_mask);
  sigaction(SIGINT, &action, NULL);
  sigaction(SIGTERM, &action, NULL);
  sigaction(SIGHUP, &action, NULL);

  pid_t pid = fork();
  if (pid < 0) fail("fork");
  if (pid == 0) {
    signal(SIGINT, SIG_DFL);
    signal(SIGTERM, SIG_DFL);
    signal(SIGHUP, SIG_DFL);
    if (setgroups(0, NULL) != 0) fail("setgroups");
    if (setresgid(1000, 1000, 1000) != 0) fail("setresgid");
    if (setresuid(1000, 1000, 1000) != 0) fail("setresuid");
    execv(argv[1], &argv[1]);
    fail("execv");
  }

  child_pid = pid;
  int status = 0;
  while (waitpid(pid, &status, 0) < 0) {
    if (errno != EINTR) fail("waitpid");
  }
  child_pid = -1;
  remove_context();
  flock(lock_fd, LOCK_UN);
  close(lock_fd);

  if (WIFEXITED(status)) return WEXITSTATUS(status);
  if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
  return 126;
}
