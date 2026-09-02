from pathlib import Path

from setuptools import find_packages, setup


ROOT = Path(__file__).parent

setup(
    # Must match the Python package name below: Pioreactor looks the plugin
    # up with importlib.metadata.metadata(<entry point name>), so the
    # distribution name and the pioreactor.plugins entry point name have to
    # normalise to the same string or get_plugins() cannot load us at all.
    name="turbidvision-pioreactor",
    version="0.1.0",
    license="MIT",
    license_files=("LICENSE.txt",),
    description="Reacgen Biosystems Turbid Vision Probe support for the Pioreactor.",
    long_description=(ROOT / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="Reacgen Biosystems",
    url="https://github.com/reacgen/turbid-vision-probe-Pioreactor-plugin-by-Reacgen-Biosystems",
    packages=find_packages(),
    include_package_data=False,
    package_data={
        "turbidvision_pioreactor": [
            "additional_sql.sql",
            "post_install.sh",
            "pre_uninstall.sh",
            "lighttpd/*.conf",
            "sudoers/*",
            "systemd/*.service",
            "systemd/*.timer",
            "ui/jobs/*.yaml",
        ]
    },
    # Deliberately empty. Plugins are installed offline from a USB drive on many
    # sites, where pip cannot fetch anything. Everything used here ships with
    # Pioreactor's own worker requirements (Adafruit-Blinka, busdevice).
    install_requires=[],
    entry_points={
        "pioreactor.plugins": "turbidvision_pioreactor = turbidvision_pioreactor"
    },
    python_requires=">=3.11",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
    ],
    project_urls={
        "Documentation": "https://github.com/reacgen/turbid-vision-probe-Pioreactor-plugin-by-Reacgen-Biosystems#readme",
        "Issues": "https://github.com/reacgen/turbid-vision-probe-Pioreactor-plugin-by-Reacgen-Biosystems/issues",
    },
)
