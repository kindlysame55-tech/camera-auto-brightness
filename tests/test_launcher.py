"""Native launcher integration using a fake child; never opens camera or screen."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
pytestmark = pytest.mark.skipif(os.name != "nt" or not COMPILER.exists(), reason="Windows .NET compiler required")


@pytest.fixture(scope="module")
def native_binaries(tmp_path_factory):
    build = tmp_path_factory.mktemp("native-launcher-build")
    launcher = build / "start-camab.exe"
    compiler_env = dict(os.environ, TEMP=str(build), TMP=str(build))
    # Fake children have no hardware requirement; exercise dispatch separately
    # from the production integrity guard in this labelled test directory.
    subprocess.run([str(COMPILER), "/nologo", "/target:winexe", "/define:LAUNCHER_TEST", "/reference:System.Windows.Forms.dll",
                    f"/out:{launcher}", str(ROOT / "packaging/launcher.cs")], check=True,
                   capture_output=True, env=compiler_env)
    source = build / "fake-child.cs"
    source.write_text('''
using System;
using System.IO;
using System.Threading;
using System.Web.Script.Serialization;
class FakeChild {
    static int Main(string[] args) {
        string config = Environment.GetEnvironmentVariable("CAMAB_CONFIG");
        string flag = String.IsNullOrEmpty(config)
            ? Path.Combine(Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? Environment.GetEnvironmentVariable("USERPROFILE"), "camera-auto-brightness", "stop.flag")
            : Path.Combine(Path.GetDirectoryName(Path.GetFullPath(config)), "stop.flag");
        var data = new { args = args, cwd = Environment.CurrentDirectory,
            temp = Environment.GetEnvironmentVariable("TEMP"), tmp = Environment.GetEnvironmentVariable("TMP"),
            config = config, local = Environment.GetEnvironmentVariable("LOCALAPPDATA"), stop_exists = File.Exists(flag) };
        File.WriteAllText(Environment.GetEnvironmentVariable("CAMAB_TEST_REPORT"), new JavaScriptSerializer().Serialize(data));
        Console.Error.WriteLine("fake child diagnostic");
        string delay = Environment.GetEnvironmentVariable("CAMAB_TEST_DELAY");
        if (delay != null) Thread.Sleep(Int32.Parse(delay));
        string status = Environment.GetEnvironmentVariable("CAMAB_TEST_EXIT");
        return status == null ? 0 : Int32.Parse(status);
    }
}
''', encoding="utf-8")
    child = build / "CamabTray.exe"
    subprocess.run([str(COMPILER), "/nologo", "/reference:System.Web.Extensions.dll", f"/out:{child}", str(source)],
                   check=True, capture_output=True, env=compiler_env)
    return launcher, child


def arrange(tmp_path, native_binaries, layout="dist"):
    launcher, child = native_binaries
    home = tmp_path / "中文 ｄｓｈ launcher"
    home.mkdir()
    shutil.copy2(launcher, home / launcher.name)
    target = home / {"dist": "dist/CamabTray.exe", "portable": "CamabTray.exe",
                     "onedir": "dist/CamabTray/CamabTray.exe"}[layout]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(child, target)
    env = os.environ.copy()
    env.update(LOCALAPPDATA=str(tmp_path / "profile"), TEMP=str(tmp_path / "missing-temp"),
               TMP=str(tmp_path / "missing-temp"), CAMAB_CONFIG=str(tmp_path / "settings/config.json"),
               CAMAB_TEST_REPORT=str(tmp_path / "child.json"))
    return home, env


def run_launcher(home, env, args=()):
    return subprocess.run([str(home / "start-camab.exe"), *args], env=env, cwd=home.parent,
                          timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)


@pytest.mark.parametrize("layout", ["dist", "portable", "onedir"])
def test_layout_and_safe_child_environment(tmp_path, native_binaries, layout):
    home, env = arrange(tmp_path, native_binaries, layout)
    original = dict(env)
    flag = Path(env["CAMAB_CONFIG"]).parent / "stop.flag"
    flag.parent.mkdir()
    flag.write_text("stop", encoding="utf-8")
    assert run_launcher(home, env).returncode == 0
    data = json.loads(Path(env["CAMAB_TEST_REPORT"]).read_text(encoding="utf-8-sig"))
    assert data["args"] == ["tray"]
    assert Path(data["cwd"]) == home
    assert data["temp"] == data["tmp"]
    assert Path(data["temp"]).is_dir()
    assert data["config"] == env["CAMAB_CONFIG"]
    assert data["local"] == env["LOCALAPPDATA"]
    assert not data["stop_exists"] and not flag.exists()
    assert env == original
    assert "fake child diagnostic" in (Path(data["temp"]) / "launcher.log").read_text(encoding="utf-8-sig")


def test_argument_quoting_and_child_failure(tmp_path, native_binaries):
    home, env = arrange(tmp_path, native_binaries)
    env["CAMAB_TEST_EXIT"] = "7"
    args = ["--selftest", "", "中文 path", 'a"b', "C:\\space dir\\", '\\"quoted"\\']
    assert run_launcher(home, env, args).returncode == 7
    assert json.loads(Path(env["CAMAB_TEST_REPORT"]).read_text(encoding="utf-8-sig"))["args"] == args


def test_runtime_fallback_and_relative_config(tmp_path, native_binaries):
    home, env = arrange(tmp_path, native_binaries)
    blocked = tmp_path / "blocked-profile"
    blocked.write_text("not a directory")
    env["LOCALAPPDATA"] = str(blocked)
    env["CAMAB_CONFIG"] = "settings/config.json"
    flag = home / "settings/stop.flag"
    flag.parent.mkdir()
    flag.write_text("stop")
    assert run_launcher(home, env, ["--selftest"]).returncode == 0
    data = json.loads(Path(env["CAMAB_TEST_REPORT"]).read_text(encoding="utf-8-sig"))
    assert Path(data["temp"]) == home / "runtime"
    assert not flag.exists()


def test_check_never_launches_or_removes_stop_signal(tmp_path, native_binaries):
    home, env = arrange(tmp_path, native_binaries)
    flag = Path(env["CAMAB_CONFIG"]).parent / "stop.flag"
    flag.parent.mkdir()
    flag.write_text("stop")
    assert run_launcher(home, env, ["--launcher-check"]).returncode == 0
    assert not Path(env["CAMAB_TEST_REPORT"]).exists()
    assert flag.exists()


def test_missing_localappdata_matches_python_home_fallback(tmp_path, native_binaries):
    home, env = arrange(tmp_path, native_binaries)
    env.pop("LOCALAPPDATA", None)
    env.pop("CAMAB_CONFIG", None)
    env["USERPROFILE"] = str(tmp_path / "user-home")
    flag = Path(env["USERPROFILE"]) / "camera-auto-brightness/stop.flag"
    flag.parent.mkdir(parents=True)
    flag.write_text("stop")
    assert run_launcher(home, env, ["--selftest"]).returncode == 0
    data = json.loads(Path(env["CAMAB_TEST_REPORT"]).read_text(encoding="utf-8-sig"))
    assert Path(data["temp"]) == flag.parent / "runtime"
    assert not flag.exists()


def test_missing_target_returns_error_without_dialog(tmp_path, native_binaries):
    launcher, _ = native_binaries
    shutil.copy2(launcher, tmp_path / launcher.name)
    assert run_launcher(tmp_path, os.environ.copy(), ["--launcher-check"]).returncode == 1


def test_production_check_rejects_low_integrity_before_hardware(tmp_path, native_binaries):
    home, env = arrange(tmp_path, native_binaries)
    compile_env = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path))
    subprocess.run([str(COMPILER), "/nologo", "/target:winexe", "/reference:System.Windows.Forms.dll",
                    f"/out:{home / 'start-camab.exe'}", str(ROOT / "packaging/launcher.cs")],
                   check=True, capture_output=True, env=compile_env)
    result = run_launcher(home, env, ["--launcher-check"])
    log = (Path(env["LOCALAPPDATA"]) / "camera-auto-brightness/runtime/launcher.log").read_text(encoding="utf-8-sig")
    value = int(log.split("Process integrity RID: ")[1].splitlines()[0])
    assert result.returncode == (2 if value < 0x2000 else 0)
    assert not Path(env["CAMAB_TEST_REPORT"]).exists()


def test_repeat_launch_does_not_start_second_child(tmp_path, native_binaries):
    home, env = arrange(tmp_path, native_binaries)
    env["CAMAB_TEST_DELAY"] = "2500"
    first = subprocess.Popen([str(home / "start-camab.exe"), "--selftest"], env=env,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 10
        while not Path(env["CAMAB_TEST_REPORT"]).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert Path(env["CAMAB_TEST_REPORT"]).exists()
        second_env = dict(env, CAMAB_TEST_REPORT=str(tmp_path / "second-child.json"))
        assert run_launcher(home, second_env, ["--selftest"]).returncode == 0
        assert not Path(second_env["CAMAB_TEST_REPORT"]).exists()
        assert first.wait(timeout=10) == 0
    finally:
        if first.poll() is None:
            first.wait(timeout=10)
