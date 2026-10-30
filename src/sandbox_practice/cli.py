from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import modal

APP_NAME = "sandbox-practice"
HERE = Path(__file__).resolve().parent


def _now() -> float:
    return time.perf_counter()


def _run(
    sb: Any, *argv: str, timeout: int | None = None
) -> tuple[int, str, str, float]:
    started = _now()
    process = sb.exec(*argv, timeout=timeout)
    stdout = process.stdout.read()
    stderr = process.stderr.read()
    process.wait()
    return process.returncode, stdout, stderr, _now() - started


def _wait_probe(sb: Any, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    code = "import json,urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765',timeout=2).read().decode())"
    last_error = "not ready"
    while time.monotonic() < deadline:
        rc, out, err, _ = _run(sb, "python", "-c", code)
        if rc == 0:
            return json.loads(out)
        last_error = err or out
        time.sleep(0.5)
    raise TimeoutError(f"guest probe did not become ready: {last_error}")


def _json_command(sb: Any, code: str) -> dict[str, Any]:
    rc, out, err, _ = _run(sb, "python", "-c", code)
    if rc:
        raise RuntimeError(err or out)
    return json.loads(out)


def _sync_navi_home(sb: Any, output_path: str | None) -> str:
    """Copy Agent state files from the guest into a local artifact directory."""
    export_code = (
        "import base64,io,pathlib,tarfile; "
        "root=pathlib.Path('/root/.navi-agent'); buf=io.BytesIO(); "
        "archive=tarfile.open(fileobj=buf,mode='w:gz'); "
        "[archive.add(p,arcname=str(p.relative_to(root))) for p in root.rglob('*') "
        "if p.is_file() and p.name!='config.yaml']; archive.close(); "
        "print(base64.b64encode(buf.getvalue()).decode())"
    )
    rc, encoded, err, _ = _run(sb, "python", "-c", export_code)
    if rc:
        raise RuntimeError(err or encoded)
    if output_path:
        output = Path(output_path)
        artifact_dir = output.parent / f"{output.stem}.artifacts" / "navi-home"
    else:
        artifact_dir = Path("results/navi-fs.artifacts/navi-home")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    archive_bytes = base64.b64decode(encoded)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        archive.extractall(artifact_dir, filter="data")
    return str(artifact_dir)


def _write_result(path: str | None, result: dict[str, Any]) -> None:
    payload = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload)
    print(payload, end="")


def _base_image() -> Any:
    return (
        modal.Image.debian_slim(python_version="3.12")
        .add_local_file(str(HERE / "guest_probe.py"), "/root/guest_probe.py")
    )


def _probe(args: argparse.Namespace) -> None:
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    image = _base_image()
    result: dict[str, Any] = {
        "experiment": "probe-memory-snapshot",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider": "modal",
        "runtime": "vm",
        "measurements": {},
        "limitations": ["snapshot byte size is not exposed by this harness/API"],
    }
    sb = None
    restored = None
    try:
        t = _now()
        sb = modal.Sandbox.create(
            "python",
            "-u",
            "/root/guest_probe.py",
            app=app,
            image=image,
            runtime="vm",
            _experimental_enable_snapshot=True,
            timeout=600,
        )
        result["measurements"]["create_seconds"] = _now() - t
        result["sandbox_id"] = sb.object_id
        before = _wait_probe(sb)
        result["measurements"]["ready_seconds_from_create_return"] = _now() - t - result["measurements"]["create_seconds"]
        result["before_snapshot"] = before
        t = _now()
        snapshot = sb._experimental_snapshot()
        result["measurements"]["snapshot_seconds"] = _now() - t
        result["snapshot_id"] = snapshot.object_id
        t = _now()
        restored = modal.Sandbox._experimental_from_snapshot(snapshot)
        result["measurements"]["restore_api_seconds"] = _now() - t
        result["restored_sandbox_id"] = restored.object_id
        t_ready = _now()
        after = _wait_probe(restored)
        result["measurements"]["restore_ready_seconds"] = _now() - t_ready
        result["measurements"]["restore_total_seconds"] = _now() - t
        result["after_restore"] = after
        result["comparisons"] = {
            "same_guest_pid": before["pid"] == after["pid"],
            "same_in_memory_boot_id": before["boot_id"] == after["boot_id"],
            "memory_counter_non_decreasing": after["ticks_in_memory"] >= before["ticks_in_memory"],
            "disk_heartbeat_present": "heartbeat" in after,
        }
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if restored is not None and not args.keep:
            try:
                restored.terminate()
            except Exception as exc:
                result["cleanup_error"] = str(exc)
        if sb is not None and not args.keep:
            try:
                sb.terminate()
            except Exception:
                pass
    _write_result(args.output, result)


def _agent_image() -> Any:
    return (
        modal.Image.debian_slim(python_version="3.12")
        .pip_install("navi-agent==0.3.1")
        .add_local_file(str(HERE / "navi_guest.py"), "/root/navi_guest.py")
    )


def _navi_fs(args: argparse.Namespace) -> None:
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    secret = modal.Secret.from_name(args.secret)
    image = _agent_image()
    result: dict[str, Any] = {
        "experiment": "navi-filesystem-snapshot",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider": "modal",
        "navi_agent": "navi-agent==0.3.1",
        "measurements": {},
    }
    sb = restored = None
    try:
        print("[navi-fs] creating sandbox", file=sys.stderr, flush=True)
        t = _now()
        sb = modal.Sandbox.create(
            "sleep", "infinity", app=app, image=image, secrets=[secret], timeout=900
        )
        result["measurements"]["create_seconds"] = _now() - t
        result["sandbox_id"] = sb.object_id
        print(f"[navi-fs] sandbox ready: {sb.object_id}", file=sys.stderr, flush=True)
        # Configure at runtime without printing or storing the credential in the host result.
        guest = (
            "import pathlib; "
            "p=pathlib.Path('/root/.navi-agent'); p.mkdir(exist_ok=True); "
            "(p/'config.yaml').write_text('runtime:\\n  max_iterations: 8\\ntelemetry:\\n  langfuse:\\n    enabled: false\\n')"
        )
        rc, out, err, setup_seconds = _run(sb, "python", "-c", guest)
        if rc:
            raise RuntimeError(err or out)
        result["measurements"]["config_setup_seconds"] = setup_seconds
        print("[navi-fs] running Navi Agent (120s command timeout)", file=sys.stderr, flush=True)
        t = _now()
        rc, out, err, agent_seconds = _run(
            sb,
            "navi-agent",
            "--yolo",
            "--session-id",
            args.session_id,
            args.prompt,
            timeout=120,
        )
        result["measurements"]["agent_run_seconds"] = agent_seconds
        result["agent_returncode"] = rc
        result["agent_stdout_bytes"] = len(out.encode())
        result["agent_stderr_bytes"] = len(err.encode())
        # Keep the model's actual response and CLI diagnostics with the local result.
        result["agent_stdout"] = out
        result["agent_stderr"] = err
        result["measurements"]["agent_call_wall_seconds"] = _now() - t
        snapshot_stats = (
            "import hashlib,json,pathlib; p=pathlib.Path('/root/.navi-agent'); "
            "files=[{'path':str(x.relative_to(p)),'bytes':x.stat().st_size,'sha256':hashlib.sha256(x.read_bytes()).hexdigest()} for x in p.rglob('*') if x.is_file() and x.name!='config.yaml']; "
            "files.sort(key=lambda x:x['path']); "
            "print(json.dumps({'files':files,'total_bytes':sum(x['bytes'] for x in files),'state_db_bytes':(p/'state.db').stat().st_size if (p/'state.db').exists() else 0}))"
        )
        before = _json_command(sb, snapshot_stats)
        result["before_snapshot"] = before
        print("[navi-fs] creating filesystem snapshot", file=sys.stderr, flush=True)
        t = _now()
        image_snapshot = sb.snapshot_filesystem(ttl=24 * 3600)
        result["measurements"]["snapshot_seconds"] = _now() - t
        result["snapshot_id"] = image_snapshot.object_id
        sb.terminate()
        sb = None
        print("[navi-fs] restoring snapshot", file=sys.stderr, flush=True)
        t = _now()
        restored = modal.Sandbox.create(app=app, image=image_snapshot, timeout=600)
        result["measurements"]["restore_create_seconds"] = _now() - t
        result["restored_sandbox_id"] = restored.object_id
        after = _json_command(restored, snapshot_stats)
        result["after_restore"] = after
        result["synced_artifact_dir"] = _sync_navi_home(restored, args.output)
        before_by_path = {item["path"]: item for item in before["files"]}
        after_by_path = {item["path"]: item for item in after["files"]}
        before_paths = set(before_by_path)
        after_paths = set(after_by_path)
        result["comparisons"] = {
            "state_db_bytes_equal": before["state_db_bytes"] == after["state_db_bytes"],
            "file_paths_equal": before_paths == after_paths,
            "file_sizes_equal": before_by_path.keys() == after_by_path.keys()
            and all(before_by_path[path]["bytes"] == after_by_path[path]["bytes"] for path in before_paths),
            "file_contents_equal": before_by_path.keys() == after_by_path.keys()
            and all(before_by_path[path]["sha256"] == after_by_path[path]["sha256"] for path in before_paths),
            "files_missing_after_restore": sorted(before_paths - after_paths),
            "files_added_after_restore": sorted(after_paths - before_paths),
            "files_with_content_changes": sorted(
                path for path in before_paths & after_paths
                if before_by_path[path]["sha256"] != after_by_path[path]["sha256"]
            ),
        }
        result["note"] = "filesystem snapshot validates on-disk Agent state; it does not restore the finished navi-agent process"
        print("[navi-fs] experiment complete", file=sys.stderr, flush=True)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if restored is not None and not args.keep:
            try:
                restored.terminate()
            except Exception as exc:
                result["cleanup_error"] = str(exc)
        if sb is not None and not args.keep:
            try:
                sb.terminate()
            except Exception:
                pass
    _write_result(args.output, result)


def _navi_fs_resume(args: argparse.Namespace) -> None:
    """Start a new Navi process from an existing filesystem snapshot/session."""
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    secret = modal.Secret.from_name(args.secret)
    result: dict[str, Any] = {
        "experiment": "resume-navi-session-from-filesystem-snapshot",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider": "modal",
        "snapshot_id": args.snapshot_id,
        "session_id": args.session_id,
        "navi_agent": "navi-agent==0.3.1",
        "measurements": {},
        "note": "Starts a new Navi process from restored files; this does not resume the old process.",
    }
    sb = None
    try:
        print("[navi-fs-resume] restoring filesystem snapshot", file=sys.stderr, flush=True)
        t = _now()
        image = modal.Image.from_id(args.snapshot_id)
        sb = modal.Sandbox.create(app=app, image=image, secrets=[secret], timeout=600)
        result["measurements"]["restore_create_seconds"] = _now() - t
        result["restored_sandbox_id"] = sb.object_id
        stats_code = (
            "import hashlib,json,pathlib; p=pathlib.Path('/root/.navi-agent'); "
            "files=[{'path':str(x.relative_to(p)),'bytes':x.stat().st_size,'sha256':hashlib.sha256(x.read_bytes()).hexdigest()} for x in p.rglob('*') if x.is_file() and x.name!='config.yaml']; "
            "files.sort(key=lambda x:x['path']); "
            "print(json.dumps({'files':files,'state_db_bytes':(p/'state.db').stat().st_size if (p/'state.db').exists() else 0}))"
        )
        before = _json_command(sb, stats_code)
        result["before_followup"] = before
        print("[navi-fs-resume] running follow-up in the same session", file=sys.stderr, flush=True)
        t = _now()
        rc, out, err, duration = _run(
            sb, "navi-agent", "--yolo", "--session-id", args.session_id,
            args.prompt, timeout=120,
        )
        result["measurements"]["agent_run_seconds"] = duration
        result["agent_returncode"] = rc
        result["agent_stdout"] = out
        result["agent_stderr"] = err
        result["agent_stdout_bytes"] = len(out.encode())
        result["agent_stderr_bytes"] = len(err.encode())
        after = _json_command(sb, stats_code)
        result["after_followup"] = after
        before_by_path = {item["path"]: item for item in before["files"]}
        after_by_path = {item["path"]: item for item in after["files"]}
        before_paths, after_paths = set(before_by_path), set(after_by_path)
        result["comparisons"] = {
            "database_content_changed": (
                before_by_path.get("state.db", {}).get("sha256")
                != after_by_path.get("state.db", {}).get("sha256")
            ),
            "file_paths_equal": before_paths == after_paths,
            "files_missing": sorted(before_paths - after_paths),
            "files_added": sorted(after_paths - before_paths),
            "files_with_content_changes": sorted(
                path for path in before_paths & after_paths
                if before_by_path[path]["sha256"] != after_by_path[path]["sha256"]
            ),
        }
        result["synced_artifact_dir"] = _sync_navi_home(sb, args.output)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sb is not None:
            try:
                sb.terminate()
            except Exception as exc:
                result["cleanup_error"] = str(exc)
    _write_result(args.output, result)


def _navi_memory(args: argparse.Namespace) -> None:
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    secret = modal.Secret.from_name(args.secret)
    image = _agent_image()
    result: dict[str, Any] = {
        "experiment": "navi-running-memory-snapshot",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider": "modal",
        "navi_agent": "navi-agent==0.3.1",
        "measurements": {},
        "note": "Navi Agent is the sandbox's primary command's child process; outcome is empirical.",
    }
    sb = restored = None
    env = {
        "NAVI_HOME": "/root/.navi-agent",
        "NAVI_PRACTICE_PROMPT": args.prompt,
        "NAVI_PRACTICE_SESSION": args.session_id,
        "NAVI_PRACTICE_POST_RUN_HOLD_SECONDS": "120",
    }
    try:
        t = _now()
        sb = modal.Sandbox.create(
            "python",
            "-u",
            "/root/navi_guest.py",
            app=app,
            image=image,
            secrets=[secret],
            env=env,
            runtime="vm",
            _experimental_enable_snapshot=True,
            timeout=900,
        )
        result["measurements"]["create_seconds"] = _now() - t
        result["sandbox_id"] = sb.object_id
        probe_code = (
            "import json,pathlib,os; p=pathlib.Path('/root/.navi-agent'); "
            "print(json.dumps({'start_marker':(p/'experiment-start.json').exists(),"
            "'exit_marker':(p/'experiment-exit.json').exists(),"
            "'state_db_bytes':(p/'state.db').stat().st_size if (p/'state.db').exists() else 0,"
            "'agent_pid':json.loads((p/'experiment-start.json').read_text()).get('agent_pid') if (p/'experiment-start.json').exists() else None,"
            "'agent_pid_alive':os.path.exists('/proc/'+str(json.loads((p/'experiment-start.json').read_text()).get('agent_pid'))) if (p/'experiment-start.json').exists() else False}))"
        )
        t = _now()
        deadline = time.monotonic() + 60
        before = None
        while time.monotonic() < deadline:
            status_code, status_out, status_err, probe_seconds = _run(
                sb, "python", "-c", probe_code
            )
            if status_code == 0:
                candidate = json.loads(status_out)
                if candidate["start_marker"]:
                    before = candidate
                    break
            time.sleep(0.5)
        if before is None:
            raise TimeoutError(f"Navi Agent did not start: {status_err or status_out}")
        time.sleep(args.snapshot_after_seconds)
        status_code, status_out, status_err, probe_seconds = _run(
            sb, "python", "-c", probe_code
        )
        result["measurements"]["pre_snapshot_probe_seconds"] = probe_seconds
        if status_code:
            raise RuntimeError(status_err or status_out)
        before = json.loads(status_out)
        result["measurements"]["wait_for_agent_start_seconds"] = _now() - t
        result["before_snapshot"] = before
        t = _now()
        snapshot = sb._experimental_snapshot()
        result["measurements"]["snapshot_seconds"] = _now() - t
        result["snapshot_id"] = snapshot.object_id
        t = _now()
        restored = modal.Sandbox._experimental_from_snapshot(snapshot)
        result["measurements"]["restore_seconds"] = _now() - t
        result["restored_sandbox_id"] = restored.object_id
        t_wait = _now()
        deadline = time.monotonic() + args.post_restore_wait_seconds
        after = None
        while time.monotonic() < deadline:
            _, out, err, probe_seconds = _run(restored, "python", "-c", probe_code)
            if out:
                after = json.loads(out)
                if after["exit_marker"]:
                    break
            elif err:
                result["restore_probe_stderr"] = err[-1000:]
            time.sleep(1)
        result["measurements"]["restore_probe_seconds"] = probe_seconds
        result["measurements"]["restore_to_agent_exit_seconds"] = _now() - t_wait
        if after is not None:
            result["after_restore"] = after
        result["comparisons"] = {
            "navi_process_visible_before_snapshot": before["agent_pid_alive"],
            "navi_process_visible_after_restore": after.get("agent_pid_alive") if after else None,
            "agent_finished_before_snapshot": before["exit_marker"],
            "agent_finished_after_restore": after.get("exit_marker") if after else None,
        }
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if restored is not None and not args.keep:
            try:
                restored.terminate()
            except Exception as exc:
                result["cleanup_error"] = str(exc)
        if sb is not None and not args.keep:
            try:
                sb.terminate()
            except Exception:
                pass
    _write_result(args.output, result)


def _resume_navi(args: argparse.Namespace) -> None:
    result: dict[str, Any] = {
        "experiment": "resume-existing-navi-memory-snapshot",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider": "modal",
        "snapshot_id": args.snapshot_id,
        "measurements": {},
    }
    restored = None
    try:
        t = _now()
        snapshot = modal.SandboxSnapshot.from_id(args.snapshot_id)
        restored = modal.Sandbox._experimental_from_snapshot(snapshot)
        result["measurements"]["restore_api_seconds"] = _now() - t
        result["restored_sandbox_id"] = restored.object_id
        probe_code = (
            "import json,pathlib,os; p=pathlib.Path('/root/.navi-agent'); "
            "s=p/'experiment-start.json'; e=p/'experiment-exit.json'; "
            "start=json.loads(s.read_text()) if s.exists() else {}; "
            "print(json.dumps({'start_marker':s.exists(),'exit_marker':e.exists(),"
            "'exit':json.loads(e.read_text()) if e.exists() else None,"
            "'agent_pid':start.get('agent_pid'),"
            "'agent_pid_alive':os.path.exists('/proc/'+str(start.get('agent_pid'))) if start.get('agent_pid') else False,"
            "'state_db_bytes':(p/'state.db').stat().st_size if (p/'state.db').exists() else 0}))"
        )
        t_ready = _now()
        deadline = time.monotonic() + args.timeout
        latest: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            latest = _json_command(restored, probe_code)
            if latest["exit_marker"]:
                break
            time.sleep(1)
        result["measurements"]["restore_to_first_probe_seconds"] = _now() - t_ready
        result["final_state"] = latest
        result["agent_completed"] = bool(latest and latest["exit_marker"])
        result["agent_process_alive_at_end"] = bool(latest and latest["agent_pid_alive"])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if restored is not None and not args.keep:
            try:
                restored.terminate()
            except Exception as exc:
                result["cleanup_error"] = str(exc)
    _write_result(args.output, result)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sandbox-practice")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor", help="check Modal SDK/auth without starting a sandbox")
    probe = subs.add_parser("probe", help="measure Modal VM memory snapshot/restore")
    probe.add_argument("--output")
    probe.add_argument("--keep", action="store_true")
    for name in ("navi-fs", "navi-memory"):
        command = subs.add_parser(name, help="run Navi Agent lifecycle experiment")
        command.add_argument("--secret", default="navi-practice-model")
        command.add_argument("--prompt", required=True)
        command.add_argument("--session-id", default="sandbox-practice-session")
        command.add_argument("--snapshot-after-seconds", type=float, default=2)
        command.add_argument("--output")
        command.add_argument("--keep", action="store_true")
        if name == "navi-memory":
            command.add_argument("--post-restore-wait-seconds", type=float, default=30)
    fs_resume = subs.add_parser(
        "navi-fs-resume", help="continue a Navi session from a filesystem snapshot"
    )
    fs_resume.add_argument("--snapshot-id", required=True)
    fs_resume.add_argument("--secret", default="navi-practice-model")
    fs_resume.add_argument("--session-id", default="sandbox-practice-session")
    fs_resume.add_argument("--prompt", required=True)
    fs_resume.add_argument("--output")
    resume = subs.add_parser("resume-navi", help="resume an existing Navi memory snapshot")
    resume.add_argument("--snapshot-id", required=True)
    resume.add_argument("--timeout", type=float, default=60)
    resume.add_argument("--output")
    resume.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.command == "doctor":
        print(json.dumps({"modal_version": modal.__version__, "auth_check": "deferred until a sandbox operation starts"}, indent=2))
        return
    if args.command == "probe":
        _probe(args)
    elif args.command == "navi-fs":
        _navi_fs(args)
    elif args.command == "navi-fs-resume":
        _navi_fs_resume(args)
    elif args.command == "resume-navi":
        _resume_navi(args)
    else:
        _navi_memory(args)


if __name__ == "__main__":
    main()
