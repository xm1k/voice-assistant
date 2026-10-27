import subprocess
import sys
from pathlib import Path


def get_changed_files(repo_path="."):
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD^1"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # First commit — list all files tracked in HEAD
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
    return [f for f in result.stdout.strip().split("\n") if f]


def run_build_script(script_path):
    script = Path(script_path)
    if not script.is_file():
        return False

    script_dir = str(script.parent)

    try:
        print(f"🚀 Running {script} ...")
        result = subprocess.run(
            ["bash", script.name],
            check=True,
            capture_output=True,
            text=True,
            cwd=script_dir,
        )
        print(f"✅ Sucess: {script}")
        if result.stdout:
            print("stdout:\n", result.stdout)
        if result.stderr:
            print("stderr:\n", result.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed {script}")
        print("stdout:\n", e.stdout)
        print("stderr:\n", e.stderr)
        return False
    except Exception as ex:
        print(f"❌ Failed {script}: {ex}")
        return False


def prune_images():
    # Rebuilt :latest tags leave previous images untagged; current tags keep the layer cache.
    subprocess.run(["podman", "image", "prune", "-f"], check=False)


def generate_dirs_list(paths, repo_path="."):
    levels = []

    for file_path in paths:
        parts = Path(file_path).parts

        for depth in range(1, len(parts)):
            if len(levels) < depth:
                levels.append(set())

            sub_path = Path(repo_path) / Path(*parts[:depth])

            if sub_path.is_dir():
                levels[depth - 1].add(str(sub_path))

    return levels[::-1]


def main():
    repo_path = sys.argv[1]

    changed = get_changed_files(repo_path)
    target_dirs = generate_dirs_list(changed, repo_path)

    for level in target_dirs:
        for directory in level:
            print("Try run: " + directory + "/build.sh")
            run_build_script(directory + "/build.sh")

    prune_images()


if __name__ == "__main__":
    main()
