from setuptools import setup

setup(
    name="translate_md",
    version="0.1.0",
    py_modules=["translate_md"],
    install_requires=["Click", "mistletoe", "python-frontmatter", "deepl"],
    entry_points={
        "console_scripts": [
            "translate_md = translate_md:translate_markdown_files",
        ],
    },
)
