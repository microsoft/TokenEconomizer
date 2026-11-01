"""One file or folder name for a repository's state, whatever its label holds."""
import re

_UNSAFE = re.compile(r'[<>:"/\\|?*]+')


def repo_filename(repo):
    """`owner/repo` becomes `owner__repo`. A label holding a Windows path stays one name: joined as it
    came, its separators or drive would file the state somewhere else, or nowhere. Only trailing
    dots and spaces go (Windows refuses them): a leading dot stays, so `.dotfiles` keeps its file."""
    return _UNSAFE.sub("__", repo).rstrip(". ") or "repo"
