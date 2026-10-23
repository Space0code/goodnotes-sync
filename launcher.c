// goodnotes-sync launcher: a tiny binary the launchd agent runs, so you can grant *it* (and nothing else)
// Full Disk Access; macOS blocks background jobs from iCloud Drive otherwise. It runs
// /usr/bin/python3 <its own folder>/goodnotes_sync.py <args> as a child process, which inherits the
// launcher's permissions (an exec would replace the launcher and lose them).
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;

int main(int argc, char *argv[]) {
    char exe[PATH_MAX], real[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real))
        return 70;
    snprintf(script, sizeof script, "%s/goodnotes_sync.py", dirname(real));

    char **args = calloc(argc + 2, sizeof *args);
    args[0] = "/usr/bin/python3";
    args[1] = script;
    for (int i = 1; i < argc; i++)
        args[i + 1] = argv[i];

    pid_t pid;
    int err = posix_spawn(&pid, args[0], NULL, NULL, args, environ);
    if (err) {
        fprintf(stderr, "goodnotes-sync: cannot start python3: %s\n", strerror(err));
        return 71;
    }
    int status;
    while (waitpid(pid, &status, 0) < 0)
        ;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
