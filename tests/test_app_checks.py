"""Static app checks: each rule's hit and its near-miss, test code exempt, and BUILD
FAILing on a hit with a fix-loop-actionable finding."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.gates.app_checks import (
    APP_CHECKS_FAILED,
    app_check_constraints,
    app_check_findings,
    app_check_language,
)
from foundry.gates.build import BuildGate
from foundry.gates.profiles import PROFILES, VerificationProfile
from foundry.schemas.gates import GateStatus


def _rules(tmp_path: Path, files: dict[str, str], language: str = "python") -> list[str]:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return [
        f.split(":", 1)[0].removeprefix("app check ")
        for f in app_check_findings(tmp_path, language)
    ]


_FLASK = "from flask import Flask, request\napp = Flask(__name__)\n"


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        (
            'import os\nSECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")\n',
            "hardcoded-secret",
        ),
        ('app.config["SECRET_KEY"] = "change-me"\n', "hardcoded-secret"),
        ('key = os.getenv("JWT_SECRET") or "x"\n', "hardcoded-secret"),
        (_FLASK + 'import sqlite3\nconn = sqlite3.connect("app.db")\n', "sqlite-threads"),
        ('import sqlite3\nconn = sqlite3.connect(":memory:")\n', "memory-db"),
        (
            "import hashlib\nh = hashlib.sha256(password.encode()).hexdigest()\n",
            "weak-password-hash",
        ),
        (
            "import hashlib\ndef hash_password(raw):\n"
            "    return hashlib.md5(raw.encode()).hexdigest()\n",
            "weak-password-hash",
        ),
        ('cur.execute(f"SELECT * FROM notes WHERE id = {note_id}")\n', "sql-format"),
        ('cur.execute("SELECT * FROM notes WHERE id = %s" % note_id)\n', "sql-format"),
        (
            'cur.execute("SELECT * FROM notes WHERE title LIKE ?", (f"%{q}%",))\n',
            "like-unescaped",
        ),
        (
            _FLASK + '@app.route("/n", methods=["POST"])\ndef add():\n    t = request.form["t"]\n',
            "csrf-missing",
        ),
    ],
)
def test_a_python_footgun_is_flagged(tmp_path: Path, source: str, rule: str) -> None:
    assert rule in _rules(tmp_path, {"app/main.py": source})


@pytest.mark.parametrize(
    "source",
    [
        'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n',
        'SECRET_KEY = os.environ.get("SECRET_KEY", "")\n',
        "if password == stored:\n    pass\n",
        # sqlite3 outside a web stack (a CLI) shares no threads.
        'import sqlite3\nconn = sqlite3.connect("app.db")\n',
        _FLASK + "import sqlite3\nconn = sqlite3.connect(path, check_same_thread=False)\n",
        'DB = os.environ.get("DB", "app.db")  # tests use ":memory:"\n',
        "import hashlib\ndigest = hashlib.sha256(body).hexdigest()\n",
        'cur.execute(f"SELECT * FROM t WHERE id IN ({placeholders})", ids)\n',
        'cur.execute("SELECT * FROM notes WHERE id = ?", (note_id,))\n',
        'print(f"Please select a file from {folder}")\n',
        'cur.execute("SELECT * FROM n WHERE t LIKE ? ESCAPE \'\\\\\'", (f"%{esc(q)}%",))\n',
        _FLASK + "from flask_wtf.csrf import CSRFProtect\nCSRFProtect(app)\nx = request.form\n",
    ],
)
def test_the_safe_python_form_is_not_flagged(tmp_path: Path, source: str) -> None:
    assert _rules(tmp_path, {"app/main.py": source}) == []


def test_a_per_request_sqlite_connection_is_not_flagged(tmp_path: Path) -> None:
    get_db = (
        _FLASK + "import sqlite3\nfrom flask import g\n"
        "def get_db():\n    if 'db' not in g:\n        g.db = sqlite3.connect(DB_PATH)\n"
        "    return g.db\n"
    )
    assert _rules(tmp_path / "a", {"app/main.py": get_db}) == []
    singleton = (
        _FLASK + "import sqlite3\n_conn = None\n"
        "def conn():\n    global _conn\n    if _conn is None:\n"
        "        _conn = sqlite3.connect(DB_PATH)\n    return _conn\n"
    )
    assert _rules(tmp_path / "b", {"app/main.py": singleton}) == ["sqlite-threads"]
    attr = _FLASK + "import sqlite3\nclass Store:\n    conn = sqlite3.connect('app.db')\n"
    assert _rules(tmp_path / "c", {"app/main.py": attr}) == ["sqlite-threads"]


def test_memory_db_is_exempt_by_test_context_not_a_substring(tmp_path: Path) -> None:
    exempt = (
        "class TestingConfig:\n    DATABASE = ':memory:'\n\n\n"
        "def make_test_app():\n    return connect(':memory:')\n\n\n"
        "if app.config['TESTING']:\n    DB = ':memory:'\n"
        "DB2 = ':memory:' if testing else 'app.db'\n"
    )
    assert _rules(tmp_path / "a", {"app/config.py": exempt}) == []
    latest = "latest = connect(':memory:')\ncontest_db = ':memory:'\n"
    assert _rules(tmp_path / "b", {"app/main.py": latest}) == ["memory-db", "memory-db"]
    js = (
        "const db = process.env.NODE_ENV === 'test' ? ':memory:' : 'app.db';\n"
        "const latest = new Database(':memory:');\n"
    )
    assert _rules(tmp_path / "c", {"src/db.js": js}, "javascript") == ["memory-db"]


def test_test_code_is_never_scanned(tmp_path: Path) -> None:
    bad = 'import sqlite3\nconn = sqlite3.connect(":memory:")\nSECRET_KEY = "x"\n'
    assert _rules(tmp_path, {"tests/helpers.py": bad, "conftest.py": bad, "test_app.py": bad}) == []


def test_flask_csrf_counts_a_template_token(tmp_path: Path) -> None:
    files = {
        "app.py": _FLASK + "x = request.form\n",
        "templates/new.html": '<form method="post">{{ csrf_token() }}</form>',
    }
    assert _rules(tmp_path, files) == []


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ('const secret = process.env.SESSION_SECRET || "keyboard cat";\n', "hardcoded-secret"),
        ("app.use(session({ secret: 'keyboard cat' }));\n", "hardcoded-secret"),
        ('const db = new Database(":memory:");\n', "memory-db"),
        (
            "const h = crypto.createHash('sha256').update(password).digest('hex');\n",
            "weak-password-hash",
        ),
        ("db.prepare(`SELECT * FROM notes WHERE id = ${id}`).get();\n", "sql-format"),
        (
            'db.prepare("SELECT * FROM n WHERE t LIKE ?").all(`%${q}%`);\n',
            "like-unescaped",
        ),
        (
            "app.use(express.urlencoded({ extended: false }));\napp.post('/n', h);\n",
            "csrf-missing",
        ),
    ],
)
def test_a_javascript_footgun_is_flagged(tmp_path: Path, source: str, rule: str) -> None:
    assert rule in _rules(tmp_path, {"src/server.js": source}, "javascript")


def test_safe_javascript_is_not_flagged(tmp_path: Path) -> None:
    source = (
        "const secret = process.env.SESSION_SECRET;\n"
        "db.prepare('SELECT * FROM notes WHERE id = ?').get(id);\n"
        "const sql = `SELECT * FROM t WHERE id IN (${placeholders})`;\n"
    )
    assert (
        _rules(tmp_path, {"src/server.js": source, "src/a.test.js": ":memory:"}, "typescript") == []
    )


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ('var jwtSecret = []byte("supersecret")\n', "hardcoded-secret"),
        ('db, _ := sql.Open("sqlite3", ":memory:")\n', "memory-db"),
        ("sum := sha256.Sum256([]byte(password))\n", "weak-password-hash"),
        ('q := fmt.Sprintf("SELECT * FROM notes WHERE id = %s", id)\n', "sql-format"),
    ],
)
def test_a_go_footgun_is_flagged(tmp_path: Path, source: str, rule: str) -> None:
    assert rule in _rules(tmp_path, {"main.go": source}, "go")


def test_go_error_text_is_not_a_secret(tmp_path: Path) -> None:
    assert _rules(tmp_path, {"main.go": 'errPassword := "invalid password"\n'}, "go") == []


def test_the_language_comes_from_the_profile_hook_then_its_stack() -> None:
    assert app_check_language(None) == "python"
    assert app_check_language(PROFILES["python-app"]) == "python"
    assert app_check_language(PROFILES["node-web"]) == "javascript"
    assert app_check_language(PROFILES["rust"]) == ""
    assert app_check_language(VerificationProfile(name="stack:typescript")) == "javascript"
    assert app_check_language(VerificationProfile(name="stack:go", app_checks="")) == ""


def test_every_rule_is_a_stack_constraint() -> None:
    [python] = app_check_constraints("python")
    assert python["id"] == "PC-python-app-safety" and python["scope"] == "global"
    for rule in (
        "hardcoded-secret",
        "sqlite-threads",
        "memory-db",
        "csrf-missing",
        "like-unescaped",
    ):
        assert rule in python["text"]
    assert app_check_constraints("rust") == []
    assert "csrf-missing" not in app_check_constraints("go")[0]["text"]


def test_build_fails_on_an_app_check_hit_with_its_line(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text('SECRET_KEY = "dev"\n')
    gate = BuildGate(app_checks="python")
    obs = gate.execute(tmp_path)
    assert gate.decide(obs) is GateStatus.FAIL
    assert obs.findings[0] == APP_CHECKS_FAILED
    assert obs.findings[1].startswith("app check hardcoded-secret: app.py:1: ")
    (tmp_path / "app.py").write_text('import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n')
    clean = gate.execute(tmp_path)
    assert gate.decide(clean) is GateStatus.PASS and clean.findings == ()


def test_build_without_the_hook_skips_the_checks(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text('SECRET_KEY = "dev"\n')
    gate = BuildGate()
    assert gate.decide(gate.execute(tmp_path)) is GateStatus.PASS
