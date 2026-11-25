"""`LiveContext`: everything one hook call or CLI command needs about where it is running."""
import os
import subprocess
import sys
from dataclasses import dataclass

from ..trace import set_tool_roles
from . import repos
from .config import LiveConfig, load_config
from .paths import RepoId, repo_identity, repo_state_dir, state_root
from .store import Store


@dataclass
class LiveContext:
    host: object            # hosts.Host
    repo: RepoId
    root: str               # the state root
    store: Store            # this host's state for this repository
    config: LiveConfig
    env: dict
    unsettled: tuple = None  # (cwd, session id) until `settle` has run

    @property
    def mech_dir(self):
        """Where the mechanism classes keep their own state (their `results_root`)."""
        return self.store.ensure("mechanisms")

    @property
    def headless(self):
        return self.is_headless()

    def is_headless(self, transcript=None):
        """Whether nobody is at the agent's own UI: `mode` when set, else what the agent shows
        (its environment, its session log at `transcript`, its process)."""
        if self.config.mode == "headless":
            return True
        if self.config.mode == "interactive":
            return False
        return self.host.headless(self.env, transcript)

    @classmethod
    def build(cls, host, cwd, env=None, session_id=None, settle=True):
        """The context for `cwd`; with `session_id`, for the repository that session is pinned
        to, wherever the agent has moved since. With `settle` (see there) it is ready to use;
        a hook settles only once it knows it will act, so a call it ignores writes nothing."""
        env = dict(os.environ if env is None else env)
        root = state_root(env)
        repo = repos.pinned(root, host.name, session_id) if session_id else None
        unsettled = None
        if repo is None:
            repo = repo_identity(cwd, env)
            unsettled = (cwd, session_id)
        base = repo_state_dir(repo, host.name, env)
        ctx = cls(host=host, repo=repo, root=root, store=Store(base),
                  config=load_config(root, repo_dir=os.path.dirname(base), env=env), env=env,
                  unsettled=unsettled)
        set_tool_roles(ctx.config.tool_roles)
        if settle:
            ctx.settle()
        return ctx

    def settle(self):
        """Carry over what this checkout's earlier keys hold, and pin the session to this
        repository (`repos.settle`); once, and a no-op for a session already pinned."""
        if self.unsettled is None:
            return
        cwd, sid = self.unsettled
        self.unsettled = None
        if repos.settle(self.root, self.host.name, self.repo, cwd, self.env, session_id=sid):
            # A merge may have brought this repository its settings file.
            self.config = load_config(self.root, repo_dir=os.path.dirname(self.store.base),
                                      env=self.env)
            set_tool_roles(self.config.tool_roles)

    def spawn(self, argv, log_name):
        """Run `te <argv>` for this host and repository in a detached process, its output
        appended to `<store>/<log_name>`. A hook uses it for anything slower than a hook may
        take (folding a session, the paid judge, the repository map)."""
        # The directory holding the package: the checkout, or the bundle's lib/.
        core = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = dict(self.env)
        env["PYTHONPATH"] = core + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH")
                                    else "")
        # It reads session logs and the agents' output, which are UTF-8 whatever the locale.
        env["PYTHONUTF8"] = "1"
        cmd = [sys.executable, "-m", "tokeneconomizer.plugin.cli", *argv,
               "--host", self.host.name, "--cwd", self.repo.root]
        self.store.ensure()
        if os.name == "nt":
            from . import winspawn
            return winspawn.spawn(cmd, self.repo.root, env, self.store.path(log_name))
        log = open(self.store.path(log_name), "a", encoding="utf-8")
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             cwd=self.repo.root, env=env, start_new_session=True)
        finally:
            log.close()
        return True
