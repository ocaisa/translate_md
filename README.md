# `translate_md`: Translating technical markdown files

Machine translation of technical markdown files can be difficult as you need to ensure that:
* the markdown syntax remains valid,
* code blocks and code within the text are still correct,
* you may need to retain specific terminology.

This package aims to enable machine translation while respecting these requirements. It translates
only the prose content of the lesson (so headings and paragraphs, ignoring all code) and allows you
to use a CSV format glossary to define specific translations (such as terminology).

The application uses DeepL as the translation backend, but could be extended to support other APIs.

## Installation

As the code is being developed, you can install the package with (for example)
```
python -m pip install --user git+https://github.com/ocaisa/translate_md.git
```

## Options

`translate_md` is under active development right now, so rather than outline the available options
it is probably better to just let the program tell you:
```
translate_md --help
```
