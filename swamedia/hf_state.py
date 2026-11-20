"""Private Hugging Face dataset adapter. Refuses to write unless the repo is PRIVATE."""
from __future__ import annotations
import json, os, tempfile


class PublicRepoError(RuntimeError):
    pass


class HFStore:
    def __init__(self, repo: str, token: str, api=None):
        if not repo:
            raise ValueError("HF_REPO is required (no default)")
        self.repo = repo
        if api is None:
            from huggingface_hub import HfApi
            api = HfApi(token=token)
        self.api = api

    def ensure_private(self):
        info = self.api.repo_info(self.repo, repo_type="dataset")
        if getattr(info, "private", False) is not True:
            raise PublicRepoError(f"STOP: dataset {self.repo} is not private. Make it private yourself; this tool never changes visibility.")

    def load_json(self, path: str, default):
        self.ensure_private()
        try:
            p = self.api.hf_hub_download(self.repo, path, repo_type="dataset", local_dir=tempfile.mkdtemp())
        except Exception as e:  # entry-not-found => first run
            if type(e).__name__ in {"EntryNotFoundError", "RemoteEntryNotFoundError", "FileNotFoundError"}:
                return default
            raise
        return json.load(open(p, encoding="utf-8"))

    def commit(self, videos: list[tuple[str, str]], jsons: dict[str, object], message: str):
        """videos: [(local_path, path_in_repo)]; jsons: {path_in_repo: obj}. One atomic commit (videos + manifest/state)."""
        self.ensure_private()  # before anything else, so a public repo is never written to
        from huggingface_hub import CommitOperationAdd
        tmp = tempfile.mkdtemp()
        ops = [CommitOperationAdd(path_in_repo=dst, path_or_fileobj=src) for src, dst in videos]
        for dst, obj in jsons.items():
            lp = os.path.join(tmp, dst.replace("/", "__"))
            json.dump(obj, open(lp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            ops.append(CommitOperationAdd(path_in_repo=dst, path_or_fileobj=lp))
        return self.api.create_commit(self.repo, ops, repo_type="dataset", commit_message=message)

    def verify_remote(self, path: str, size: int) -> bool:
        """Read-back: file exists in the private repo with the expected size."""
        self.ensure_private()
        infos = self.api.get_paths_info(self.repo, [path], repo_type="dataset")
        return bool(infos) and getattr(infos[0], "size", None) == size
