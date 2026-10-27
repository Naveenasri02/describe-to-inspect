"""Tamper-evident audit trail: hash-chained, append-only log of human actions; personal logins with roles."""
import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

ROLES = {"operator", "qa"}
GENESIS = "0" * 64


def _hash(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def append(self, action: str, user: str, details: dict, reason: str = "") -> dict:
        rows = self._rows()
        entry = {"seq": len(rows) + 1, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "user": user,
                 "action": action, "details": details, "reason": reason, "prev": rows[-1]["hash"] if rows else GENESIS}
        entry["hash"] = _hash(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def verify(self) -> tuple[bool, int | None]:
        prev = GENESIS
        for i, row in enumerate(self._rows(), 1):
            body = {k: v for k, v in row.items() if k != "hash"}
            if row.get("seq") != i or row.get("prev") != prev or row.get("hash") != _hash(body):
                return False, i
            prev = row["hash"]
        return True, None

    def entries(self, since: str | None = None) -> list[dict]:
        rows = self._rows()
        if since is None:
            return rows
        t = datetime.fromisoformat(since)
        return [r for r in rows if datetime.fromisoformat(r["at"]) >= t]


def _pbkdf2(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


class Users:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def add(self, name: str, role: str, password: str):
        if role not in ROLES:
            raise ValueError(f"role must be one of {sorted(ROLES)}")
        users = self._load()
        salt = secrets.token_hex(16)
        users[name] = {"role": role, "salt": salt, "hash": _pbkdf2(password, salt)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(users, indent=2), encoding="utf-8")

    def check(self, name: str, password: str) -> str | None:
        u = self._load().get(name)
        if u and secrets.compare_digest(_pbkdf2(password, u["salt"]), u["hash"]):
            return u["role"]
        return None


def config_hashes(product_dir: Path) -> dict:
    from d2i.checklist import current
    p = Path(product_dir)
    cl = current(p / "checklists.jsonl")
    model = p / "model.pt"
    return {"model_sha256": hashlib.sha256(model.read_bytes()).hexdigest() if model.exists() else None,
            "checklist_version": cl.get("version") if cl else None,
            "checklist_sha256": _hash(cl) if cl else None}


def quarantine_list(product_dir: Path) -> dict:
    from d2i.record import _lines, _ts
    p = Path(product_dir)
    tests = _lines(p / "selftests.jsonl")
    if not tests or tests[-1]["passed"]:
        return {"since": None, "failed_at": None, "strips": []}
    passed = [t for t in tests if t["passed"]]
    since = passed[-1] if passed else None
    strips = [v for v in _lines(p / "log.jsonl") if since is None or _ts(v) >= _ts(since)]
    return {"since": since["at"] if since else None, "failed_at": tests[-1]["at"], "strips": strips}


def _require_qa(users: Users, name: str, password: str) -> None:
    if users.check(name, password) != "qa":
        raise PermissionError("QA login required")


def release_quarantine(product_dir, audit: AuditLog, users: Users, name: str, password: str, reason: str) -> dict:
    _require_qa(users, name, password)
    if not reason.strip():
        raise ValueError("a reason is required")
    q = quarantine_list(product_dir)
    return audit.append("release_quarantine", name, {"strips": len(q["strips"]), "failed_at": q["failed_at"]}, reason)


def log_physical_challenge(audit: AuditLog, user: str, strip_label: str, ejected: bool) -> dict:
    return audit.append("physical_challenge", user, {"strip_label": strip_label, "ejected": ejected})


def sign_record(html: str, audit: AuditLog, users: Users, name: str, password: str,
                meaning: str = "Reviewed and approved") -> dict:
    _require_qa(users, name, password)
    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    entry = audit.append("sign", name, {"record_sha256": sha, "meaning": meaning})
    return {"signed_by": name, "role": "qa", "meaning": meaning, "at": entry["at"], "record_sha256": sha}
