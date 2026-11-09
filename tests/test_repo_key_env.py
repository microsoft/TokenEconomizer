"""`$TOKENECONOMIZER_REPO`: the repository of a session whose directory holds no git metadata."""
import os

from tokeneconomizer.plugin import paths


def git_repo(root, remote):
    os.makedirs(os.path.join(root, ".git"), exist_ok=True)
    with open(os.path.join(root, ".git", "HEAD"), "w") as fh:
        fh.write("ref: refs/heads/main\n")
    with open(os.path.join(root, ".git", "config"), "w") as fh:
        fh.write(f'[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = {remote}\n')
    return root


def make_env(tmp_path, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return dict({"TOKENECONOMIZER_HOME": str(tmp_path / "te"), "HOME": str(home)}, **extra)


def test_a_named_remote_keys_an_export_without_git_metadata_as_its_clones(tmp_path):
    """An export of a checkout in a container, or a new folder per run, holds no `.git`; the
    variable names its repository, so it shares its clones' key."""
    env = make_env(tmp_path)
    named = make_env(tmp_path, TOKENECONOMIZER_REPO="git@github.com:Microsoft/TokenEconomizer.git")
    clone = git_repo(str(tmp_path / "TokenEconomizer"), "https://github.com/microsoft/TokenEconomizer")
    export = tmp_path / "runs" / "job-1" / "work"
    export.mkdir(parents=True)
    r = paths.repo_identity(str(export), named)
    assert r.key == paths.repo_identity(clone, env).key
    assert r.how == paths.BY_REMOTE and r.root == str(export)
    assert r.label == "microsoft/tokeneconomizer" and r.remote == "github.com/microsoft/tokeneconomizer"
    # Another run's folder gets the same key; without the variable each folder is its own.
    other = tmp_path / "runs" / "job-2" / "work"
    other.mkdir(parents=True)
    assert paths.repo_identity(str(other), named).key == r.key
    plain = paths.repo_identity(str(other), env)
    assert plain.how == paths.BY_DIR and plain.key != r.key


def test_an_empty_value_changes_nothing_and_a_set_one_names_the_repository(tmp_path):
    env = make_env(tmp_path)
    export = tmp_path / "work"
    export.mkdir()
    assert paths.repo_identity(str(export), make_env(tmp_path, TOKENECONOMIZER_REPO="")).how == paths.BY_DIR
    named = make_env(tmp_path, TOKENECONOMIZER_REPO="https://github.com/microsoft/TokenEconomizer")
    elsewhere = git_repo(str(tmp_path / "elsewhere"), "https://example.org/team/elsewhere.git")
    assert paths.repo_identity(elsewhere, named).key == paths.repo_identity(str(export), named).key
    assert paths.repo_identity(elsewhere, env).key != paths.repo_identity(elsewhere, named).key
