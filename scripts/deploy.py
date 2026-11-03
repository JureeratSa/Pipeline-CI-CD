"""
Deploy โปรเจกต์ที่รันด้วย docker compose ด้วยคำสั่งเดียว — รันที่โฟลเดอร์ของโปรเจกต์ (ที่มี docker-compose.yml)

    python deploy.py            ดึงโค้ดล่าสุด → build → สลับ → ตรวจสุขภาพ (พังแล้วย้อนกลับให้เอง)
    python deploy.py --check    ทำถึงขั้น build + ตรวจ แต่ไม่สลับของที่รันอยู่ (ซ้อมได้ทุกเมื่อ)
    python deploy.py --no-pull  ใช้โค้ดที่อยู่ในโฟลเดอร์ตอนนี้ ไม่ git pull

ขั้นตอน (ล้มขั้นไหน หยุดทันที — ก่อนขั้นที่ 6 ระบบที่รันอยู่ไม่ถูกแตะเลย):
  1. Docker ต้องทำงานอยู่ และโฟลเดอร์ต้องไม่มีไฟล์ที่ track แล้วถูกแก้ค้างไว้
  2. git pull --ff-only
  3. รันคำสั่งตรวจก่อน deploy (PRE_CHECK เช่น ตรวจไฟล์ migration) — ไม่ตั้ง = ข้าม
  4. จำ image ที่รันอยู่ไว้เป็น :rollback แล้ว docker compose build
  5. ถ้าใช้ Alembic (ALEMBIC_SERVICE): ฐานข้อมูลต้องอยู่ที่ migration ล่าสุดแล้ว — สคริปต์ไม่รัน upgrade ให้เอง
  6. docker compose up -d → รอทุก container พร้อม (+ HEALTH_URL ตอบ 200 ถ้าตั้งไว้)
  7. ไม่ผ่าน → ย้อนกลับไปใช้ image :rollback
ผลทุกครั้งบันทึกต่อท้าย HISTORY_FILE (เวลา, commit, ผล)

ชื่อโปรเจกต์และรายการ service อ่านจาก docker-compose.yml เอง — ปกติแก้แค่ค่าในส่วน "ตั้งค่า" ด้านล่าง
ค่าแต่ละตัวทับได้ด้วย env var ชื่อเดียวกันขึ้นต้นด้วย DEPLOY_ เช่น DEPLOY_HEALTH_URL
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# ───────────── ตั้งค่า ─────────────
# URL ที่ต้องตอบ 200 หลัง deploy (เช่น http://127.0.0.1:8000/health) — ว่าง = ดูแค่ healthcheck ของ container
HEALTH_URL = os.getenv("DEPLOY_HEALTH_URL", "")
# รอได้นานสุดกี่วินาที (ตั้งตามเวลาที่ระบบใช้ start จริง + เผื่อ)
HEALTH_TIMEOUT = int(os.getenv("DEPLOY_HEALTH_TIMEOUT", "300"))
# ชื่อ service ที่มี Alembic (เช่น backend) — ว่าง = โปรเจกต์ไม่ใช้ Alembic ข้ามการตรวจ
ALEMBIC_SERVICE = os.getenv("DEPLOY_ALEMBIC_SERVICE", "")
# คำสั่งตรวจก่อน build เช่น "python scripts/check_migrations.py --versions alembic/versions --models app/models.py"
PRE_CHECK = os.getenv("DEPLOY_PRE_CHECK", "")
# ไฟล์บันทึกประวัติการ deploy
HISTORY_FILE = Path(os.getenv("DEPLOY_HISTORY_FILE", str(Path.home() / "deploy-history.log")))
# ──────────────────────────────────

PROJECT_DIR = Path.cwd()
# ปิด maintenance/gc อัตโนมัติของ git — บนไดรฟ์เครือข่ายของ Windows ทำแล้ว error
GIT = ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0"]


class DeployError(Exception):
    pass


def run(cmd: list[str], check: bool = True, quiet: bool = False) -> str:
    if not quiet:
        print(f"  $ {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise DeployError(f"คำสั่งล้ม: {' '.join(cmd)}\n{result.stdout}{result.stderr}".rstrip())
    return result.stdout.strip()


def step(title: str) -> None:
    print(f"\n▶ {title}", flush=True)


def read_compose() -> tuple[str, dict[str, tuple[str, str]], list[str]]:
    """คืน (ชื่อโปรเจกต์, {service ที่ build เอง: (ชื่อ image, tag)}, ทุก service) จาก docker compose config"""
    # quiet: ผลของ config มีค่าจาก .env อยู่ด้วย — ไม่พิมพ์ออกจอ
    cfg = json.loads(run(["docker", "compose", "config", "--format", "json"], quiet=True))
    name = cfg["name"]
    built = {}
    for service, spec in cfg["services"].items():
        if "build" in spec:
            image = spec.get("image") or f"{name}-{service}"
            repo, _, tag = image.rpartition(":")
            # "registry:5000/app" → ':' นั้นเป็น port ไม่ใช่ tag
            built[service] = (repo, tag) if repo and "/" not in tag else (image, "latest")
    return name, built, list(cfg["services"])


def container_id(service: str) -> str:
    return run(["docker", "compose", "ps", "-q", service], check=False, quiet=True)


def remember_running_images(built: dict[str, tuple[str, str]]) -> None:
    """ติดป้าย :rollback ให้ image ที่ container ใช้อยู่จริง (ไม่ใช่ :latest — อาจถูก build ใหม่ไว้แต่ยังไม่ได้สลับ)"""
    for service, (image, tag) in built.items():
        cid = container_id(service)
        if not cid:
            print(f"  {service}: ไม่ได้รันอยู่ — ไม่มีของให้ย้อนกลับ")
            continue
        image_id = run(["docker", "inspect", "-f", "{{.Image}}", cid])
        try:
            run(["docker", "image", "tag", image_id, f"{image}:rollback"])
            continue
        except DeployError:
            pass
        # image ของ container ถูกลบไปแล้ว (build ใหม่ทับโดยไม่ได้สลับ — Docker แบบ containerd ลบ image ที่ไม่มีชื่อทิ้ง)
        print(f"  ⚠ {service}: ไม่เหลือ image ที่รันอยู่ — ใช้ {image}:{tag} (ก่อน build) เป็นจุดย้อนกลับ")
        run(["docker", "image", "tag", f"{image}:{tag}", f"{image}:rollback"])


def wait_healthy(services: list[str]) -> None:
    """รอจนทุก container พร้อม: มี healthcheck = ต้อง healthy, ไม่มี = ต้อง running"""
    deadline = time.time() + HEALTH_TIMEOUT
    while True:
        states = {}
        for service in services:
            cid = container_id(service)
            info = json.loads(run(["docker", "inspect", cid], check=False, quiet=True) or "[{}]")[0] if cid else {}
            state = info.get("State", {})
            health = state.get("Health", {}).get("Status")
            states[service] = health or ("ready" if state.get("Status") == "running" else state.get("Status", "missing"))
        if any(s in ("unhealthy", "exited", "dead") for s in states.values()):
            raise DeployError(f"container ไม่ผ่าน: {states}")
        if all(s in ("healthy", "ready") for s in states.values()):
            break
        if time.time() > deadline:
            raise DeployError(f"รอเกิน {HEALTH_TIMEOUT} วินาทีแล้วยังไม่พร้อม: {states}")
        print(f"  รอ... {states}", flush=True)
        time.sleep(10)
    if HEALTH_URL:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as resp:
            if resp.status != 200:
                raise DeployError(f"{HEALTH_URL} ตอบ {resp.status}")
    print("  ทุก container พร้อม" + (f" และ {HEALTH_URL} ตอบ 200" if HEALTH_URL else ""))


def rollback(built: dict[str, tuple[str, str]], services: list[str]) -> None:
    step("ย้อนกลับไปใช้ image ก่อน deploy")
    for image, tag in built.values():
        if run(["docker", "image", "ls", "-q", f"{image}:rollback"], check=False, quiet=True):
            run(["docker", "image", "tag", f"{image}:rollback", f"{image}:{tag}"])
    run(["docker", "compose", "up", "-d", "--no-build"])
    wait_healthy(services)


def record(commit: str, result: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{commit}\t{result}\n"
    try:
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        print(f"(บันทึกประวัติลง {HISTORY_FILE} ไม่ได้: {e})")


def deploy(args) -> str:
    step("1. ตรวจ Docker และโฟลเดอร์")
    try:
        run(["docker", "info", "--format", "{{.ServerVersion}}"], quiet=True)
    except DeployError:
        raise DeployError("Docker ไม่ได้ทำงาน — เปิด Docker ก่อน") from None
    branch = run(GIT + ["branch", "--show-current"])
    if dirty := run(GIT + ["status", "--porcelain", "--untracked-files=no"]):
        raise DeployError(f"มีไฟล์ที่แก้ค้างไว้ — commit หรือเก็บก่อน deploy:\n{dirty}")
    if branch != "main":
        print(f"  ⚠ อยู่ที่ branch '{branch}' ไม่ใช่ main")

    if not args.no_pull:
        step("2. ดึงโค้ดล่าสุด")
        run(GIT + ["pull", "--ff-only"])
    commit = run(GIT + ["log", "-1", "--format=%h %s"])
    print(f"  จะ deploy: {branch} @ {commit}")
    project, built, services = read_compose()
    print(f"  โปรเจกต์ {project}: build {', '.join(built) or '-'} / ทั้งหมด {', '.join(services)}")

    if PRE_CHECK:
        step("3. ตรวจก่อน deploy")
        print(run(shlex.split(PRE_CHECK)))

    step("4. build image ใหม่ (ระบบเดิมยังรันอยู่)")
    remember_running_images(built)
    run(["docker", "compose", "build"])

    if ALEMBIC_SERVICE:
        step("5. ตรวจว่าฐานข้อมูลอยู่ที่ migration ล่าสุด (อ่านอย่างเดียว)")
        current = run(["docker", "compose", "run", "--rm", "--no-deps", "-T", ALEMBIC_SERVICE, "alembic", "current"])
        if "(head)" not in current:
            raise DeployError(
                f"ฐานข้อมูลยังไม่อยู่ที่ migration ล่าสุด ({current or 'ไม่มี'}) — ตรวจไฟล์ migration แล้วรัน\n"
                f"  docker compose run --rm {ALEMBIC_SERVICE} alembic upgrade head\nแล้ว deploy ใหม่"
            )
        print(f"  {current}")

    if args.check:
        return f"{commit} — ตรวจผ่าน (--check: ไม่ได้สลับ)"

    step("6. สลับไปใช้ image ใหม่")
    run(["docker", "compose", "up", "-d", "--no-build"])
    try:
        wait_healthy(services)
    except (DeployError, OSError) as e:
        print(f"\n❌ ระบบใหม่ไม่ผ่าน: {e}")
        try:
            rollback(built, services)
        except (DeployError, OSError) as e2:
            raise DeployError(f"ย้อนกลับไม่สำเร็จ — ต้องแก้มือทันที: {e2}") from e
        raise DeployError(
            f"deploy ไม่ผ่าน ย้อนกลับไปใช้ image เดิมแล้ว (ระบบใช้งานได้) — โค้ดในโฟลเดอร์ยังเป็น {commit}\n"
            "  ดูสาเหตุ: docker compose logs --tail 100"
        ) from e
    return f"{commit} — สำเร็จ"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="ตรวจ + build อย่างเดียว ไม่สลับของที่รันอยู่")
    parser.add_argument("--no-pull", action="store_true", help="ไม่ git pull ใช้โค้ดในโฟลเดอร์ตอนนี้")
    args = parser.parse_args()
    started = time.time()
    try:
        result = deploy(args)
    except DeployError as e:
        print(f"\n❌ {e}")
        record(run(GIT + ["log", "-1", "--format=%h"], check=False, quiet=True), f"ล้ม: {str(e).splitlines()[0]}")
        return 1
    print(f"\n✅ {result} ({time.time() - started:.0f} วินาที)")
    record(result.split(" ", 1)[0], result.split(" — ", 1)[1])
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # console ของ Windows แสดงภาษาไทย/อีโมจิได้
    sys.exit(main())
