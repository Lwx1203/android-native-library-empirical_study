import subprocess
import time
import os
import threading

# ======================
apk_dir = r''
output_base_dir = r''
test_duration_sec = 7200
aapt_path = r""
ENABLE_REBOOT = True
# ======================

def install_apk(apk_path):
    subprocess.run(["adb", "install", "-r", apk_path], check=True)
    subprocess.run(["adb", "shell", "settings", "put", "global", "policy_control", "immersive.full=*"])


def clear_logcat():
    subprocess.run(["adb", "logcat", "-c"], check=True)

def get_package_name_from_apk(apk_path):
    result = subprocess.run(
        [aapt_path, "dump", "badging", apk_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8"
    )
    for line in result.stdout.splitlines():
        if line.startswith("package:"):
            for part in line.split():
                if part.startswith("name="):
                    package_name = part.split("=")[1].strip("'")
                    print(f"Package name: {package_name}")
                    return package_name
    raise Exception(f"Failed to extract package name: {apk_path}")

def grant_all_permissions(package_name):
    print(f"Grant all permissions to {package_name}")
    result = subprocess.run(
        ["adb", "shell", "dumpsys", "package", package_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        encoding="utf-8"
    )
    permissions = []
    capture = False
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("requested permissions:"):
            capture = True
        elif capture:
            if line.startswith("install permissions:") or line.startswith("User 0:"):
                break
            if line.startswith("android.permission"):
                permissions.append(line)
    for perm in permissions:
        subprocess.run(["adb", "shell", "pm", "grant", package_name, perm],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def run_logcat(log_path):
    print(f"Recording logcat to {log_path}")
    f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(["adb", "logcat"], stdout=f, stderr=subprocess.STDOUT)
    return proc, f

def bring_app_to_front(package_name):
    subprocess.run([
        "adb", "shell", "monkey", "-p", package_name,
        "-c", "android.intent.category.LAUNCHER", "1"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def foreground_monitor(package_name, stop_event):
    print("Starting foreground monitoring thread")
    while not stop_event.is_set():
        result = subprocess.run(
            ["adb", "shell", "dumpsys", "window", "windows"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8"
        )
        if f"mCurrentFocus" in result.stdout and package_name not in result.stdout:
            print("⚠ App not in foreground, attempting to bring it to the foreground...")
            bring_app_to_front(package_name)
        time.sleep(2)

def kill_monkey_on_device():
    print("Terminating monkey process on the device...")
    subprocess.run(["adb", "shell", "pkill", "monkey"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def is_adb_connected():
    result = subprocess.run(["adb", "get-state"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8")
    return result.stdout.strip() == "device"


def run_monkey_with_timeout(package, throttle, timeout_sec, log_path):
    monkey_cmd = [
        "adb", "shell", "monkey",
        "-p", package,
        "--pct-syskeys", "0",
        "--throttle", str(throttle),
        "-v", "-v", "1000000000"
    ]
    with open(log_path, "w", encoding="utf-8") as f:
        proc = subprocess.Popen(monkey_cmd, stdout=f, stderr=subprocess.STDOUT)

        stop_event = threading.Event()
        monitor_thread = threading.Thread(target=foreground_monitor, args=(package, stop_event))
        monitor_thread.start()

        start_time = time.time()
        check_interval = 5

        try:
            while True:
                ret = proc.poll()
                if ret is not None:
                    break

                if (time.time() - start_time) % check_interval < 1:
                    if not is_adb_connected():
                        print("ADB disconnected, retrying in 10 seconds...")
                        time.sleep(10)
                        if not is_adb_connected():
                            print("ADB still not connected, terminating current Monkey test...")
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                            kill_monkey_on_device()
                            break

                if time.time() - start_time > timeout_sec:
                    print("Timeout reached, terminating Monkey process...")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    kill_monkey_on_device()
                    break

                time.sleep(1)

        finally:
            stop_event.set()
            monitor_thread.join()

    print("Monkey test completed")


def uninstall_package(package_name):
    print(f"Uninstalling app: {package_name}")
    result = subprocess.run(["adb", "uninstall", package_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8")
    if "Unknown package" in result.stderr:
        print(f"App {package_name} is not installed, no need to uninstall.")

# === Wait for device reboot ===
def wait_for_device(timeout=180):
    start_time = time.time()
    print("Waiting for device to reboot...")
    while True:
        result = subprocess.run(["adb", "wait-for-device"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            boot_check = subprocess.run(
                ["adb", "shell", "getprop", "sys.boot_completed"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8"
            )
            if boot_check.stdout.strip() == "1":
                print("Device boot completed")
                break
        if time.time() - start_time > timeout:
            print("Device reboot timeout")
            break
        time.sleep(5)

def test_single_apk_with_output_dir(apk_path, output_dir):
    print("\n" + "="*60)
    print(f"Starting test for APK: {apk_path}")
    os.makedirs(output_dir, exist_ok=True)
    apk_name = os.path.splitext(os.path.basename(apk_path))[0]
    monkey_log_path = os.path.join(output_dir, "monkey_log.txt")
    logcat_log_path = os.path.join(output_dir, "logcat_output.txt")

    try:
        package_name = get_package_name_from_apk(apk_path)
    except Exception as e:
        print(e)
        return

    try:
        uninstall_package(package_name)
        install_apk(apk_path)
    except subprocess.CalledProcessError:
        print(f"Installation failed, skipping {apk_path}")
        return

    grant_all_permissions(package_name)
    clear_logcat()
    logcat_proc, logcat_file = run_logcat(logcat_log_path)
    time.sleep(1)

    run_monkey_with_timeout(package_name, throttle=100, timeout_sec=test_duration_sec, log_path=monkey_log_path)

    time.sleep(2)
    logcat_proc.terminate()
    logcat_proc.wait()
    logcat_file.close()

    uninstall_package(package_name)
    print(f"Test completed, logs saved to: {output_dir}")

    if ENABLE_REBOOT:
        print("Rebooting device...")
        subprocess.run(["adb", "reboot"])
        wait_for_device()


def main():
    if not os.path.exists(apk_dir):
        print(f"APK directory does not exist: {apk_dir}")
        return

    if ENABLE_REBOOT:
        print("Rebooting device before testing...")
        subprocess.run(["adb", "reboot"])
        wait_for_device()

    for root, dirs, files in os.walk(apk_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        apk_files = [f for f in files if f.lower().endswith(".apk") and not f.startswith(".") and not f.startswith("._")]
        if not apk_files:
            continue

        relative_path = os.path.relpath(root, apk_dir)
        current_output_dir = os.path.join(output_base_dir, relative_path)

        for apk_file in apk_files:
            apk_path_full = os.path.join(root, apk_file)
            print(f"\nPreparing test: {apk_path_full}")
            single_apk_output_dir = os.path.join(current_output_dir, os.path.splitext(apk_file)[0])
            test_single_apk_with_output_dir(apk_path_full, single_apk_output_dir)

    print("All APK tests completed!")


if __name__ == "__main__":
    main()