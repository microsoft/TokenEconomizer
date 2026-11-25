"""Run ensure_launcher from 4 threads at once, as a Hermes gateway does, N times; count errors."""
import collections, os, sys, tempfile, threading
sys.path.insert(0, sys.argv[1])
from tokeneconomizer.plugin import paths
if len(sys.argv) > 4 and sys.argv[4] == "noretry":
    from tokeneconomizer.plugin import store
    store._replace = os.replace      # the unique name alone, without the Windows retry
if len(sys.argv) > 4 and sys.argv[4] == "windows":
    paths._WINDOWS = True
plugin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugin")
runs, fails, bad = int(sys.argv[2]), collections.Counter(), 0
for _ in range(runs):
    env = {"TE_PLUGIN_ROOT": os.path.abspath(plugin), "TOKENECONOMIZER_HOME": tempfile.mkdtemp()}
    go, errs = threading.Barrier(4), []
    def w():
        go.wait()
        try: paths.ensure_launcher(env)
        except OSError as e: errs.append(type(e).__name__)
    ts = [threading.Thread(target=w) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    bad += bool(errs); fails.update(errs)
print(f"{sys.argv[3]}: {bad}/{runs} runs had an error {dict(fails)}")
