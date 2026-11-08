"""The pip package (pyproject.toml) maps the source tree to `chemassistent`;
only the GTK-free modules of the headless interface go into it, not the
desktop app (GTK, requests, websocket-client) next to them."""
from setuptools import setup
from setuptools.command.build_py import build_py

# headless.py and what it imports (directly or through agent/workspace),
# plus the runner it starts. New imports of these modules must be added here.
PACKAGE_MODULES = {
    "__init__", "headless", "agent", "config", "documents", "keystore", "issues", "api_versions",
    "lessons", "history", "workspace", "chemotion_runner",
}


class BuildPy(build_py):
    def find_package_modules(self, package, package_dir):
        return [module for module in super().find_package_modules(package, package_dir)
                if package != "chemassistent" or module[1] in PACKAGE_MODULES]


setup(cmdclass={"build_py": BuildPy})
