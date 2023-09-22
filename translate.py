import copy
import os
import sys
import deepl
from split_markdown4gpt import split
from pathlib import Path
from frontmatter import Frontmatter

import mistletoe
from mistletoe.block_token import (
    BlockToken,
    Heading,
    Paragraph,
    SetextHeading,
    ThematicBreak,
)
from mistletoe.markdown_renderer import MarkdownRenderer, BlankLine
from mistletoe.span_token import InlineCode, RawText, SpanToken


def replace_inline_code(token: SpanToken, inline_code_dict: dict):
    """Update the text contents of a span token and its children.
    `InlineCode` tokens are left unchanged."""
    if isinstance(token, InlineCode):
        # Generate a  placeholder
        placeholder = "x%03dy" % len(inline_code_dict)
        # Add the placeholder to the dict
        inline_code_dict[placeholder] = token.children[0].content
        # token.content = token.content.replace("mistletoe", "The Amazing mistletoe")
        token.children[0].content = placeholder

    if hasattr(token, "children") and not isinstance(token, InlineCode):
        for child in token.children:
            replace_inline_code(child, inline_code_dict)


def restore_inline_code(token: SpanToken, inline_code_dict: dict):
    """Update the text contents of a span token and its children.
    `InlineCode` tokens are left unchanged."""
    if isinstance(token, InlineCode):
        # token.content = token.content.replace("mistletoe", "The Amazing mistletoe")
        if token.children[0].content in inline_code_dict.keys():
            token.children[0].content = inline_code_dict.pop(token.children[0].content)

    if hasattr(token, "children") and not isinstance(token, InlineCode):
        for child in token.children:
            replace_inline_code(child, inline_code_dict)


def translate_block(
    token: BlockToken,
    renderer=None,
    ignore_triple_colon=True,
    source_lang="EN",
    target_lang=None,
    glossary=None,
    auth_key=None,
):
    """Update the text contents of paragraphs and headings within this block,
    and recursively within its children."""

    # Create a markdown renderer if we don't have one already
    if renderer is None:
        renderer = MarkdownRenderer()
    # By default just assume we return what we got
    translated_token = token
    # if isinstance(token, (Paragraph, SetextHeading, Heading)):
    if isinstance(token, (Paragraph, Heading)):
        # Ignore any paragraph that starts with ':::' (this is Carpentries Workbench specific)
        if (
            ignore_triple_colon
            and isinstance(token, Paragraph)
            and isinstance(token.children[0], RawText)
            and token.children[0].content.startswith(":::")
        ):
            pass
        else:
            # Replace all the inline code blocks with placeholders
            inline_code_dict = {}
            for child in token.children:
                replace_inline_code(child, inline_code_dict)
            # Reconstruct the resulting markdown block to give a full context to translate
            markdown_text = renderer.render(token)
            # Starting or ending with special markdown syntax seems to cause syntax loss, so let's work around that by
            # adding something that can't get translated
            # - leaving a '.' at the end can sometimes cause DeepL to remove the subsequent space
            # - Left a space as you don't want to mess with the first/last word
            start_marker = "XYZ.1 "
            end_marker = "".join(reversed(start_marker))
            markdown_text = start_marker + markdown_text + end_marker
            # Translate the resulting markdown text (method translates arrays of strings, so need to do mapping)
            translated_markdown = translate_deepl(
                [markdown_text],
                source_lang=source_lang,
                target_lang=target_lang,
                glossary=glossary,
                auth_key=auth_key,
            )[0]
            translated_markdown = translated_markdown.strip()

            # Make sure all our dict elements exists and are surrounded by ` (sometimes DeepL decides to change them)
            for key in inline_code_dict.keys():
                if key not in translated_markdown:
                    raise RuntimeError(
                        "Code placeholder %s does not appear in translation: %s"
                        % (key, translated_markdown)
                    )
                location = translated_markdown.find(key)
                translated_markdown = (
                    translated_markdown[: location - 1]
                    + "`"
                    + key
                    + "`"
                    + translated_markdown[location + len(key) + 1 :]
                )

            # Remove our markers from the translated text
            if translated_markdown.startswith(start_marker):
                translated_markdown = translated_markdown.replace(start_marker, "")
            else:
                raise RuntimeError(
                    "Translated markdown does not have our start signature (%s): %s"
                    % (start_marker, translated_markdown)
                )
            if translated_markdown.endswith(end_marker):
                translated_markdown = translated_markdown.replace(end_marker, "")
            else:
                raise RuntimeError(
                    "Translated markdown does not have our end signature (%s): %s"
                    % (end_marker, translated_markdown)
                )
            # Deconstruct the resulting markdown again and identify the token we need
            temp_document = mistletoe.Document(translated_markdown)

            for child in temp_document.children:
                # Assuming here that first paragraph is a hit
                if isinstance(child, (Paragraph, Heading)):
                    translated_token = child
                    break
            if translated_token is None:
                raise RuntimeError(
                    "Something went wrong, we didn't get translation token back: \n%s"
                    % renderer.render(document)
                )
            # Replace all the placeholders with their inline codeblocks
            for child in translated_token.children:
                restore_inline_code(child, inline_code_dict)
            if len(inline_code_dict):
                print(markdown_text)
                print(translated_markdown)
                raise RuntimeError(
                    "Something went wrong, you should have an empty dict but you have: %s"
                    % inline_code_dict
                )

    if hasattr(token, "children"):
        for id, child in enumerate(token.children):
            if isinstance(child, BlockToken):
                token.children[id] = translate_block(
                    child,
                    renderer=renderer,
                    ignore_triple_colon=ignore_triple_colon,
                    source_lang=source_lang,
                    target_lang="es",
                    glossary=glossary,
                    auth_key=auth_key,
                )

    return translated_token


DEFAULT_TOKEN_LIMIT = 4096
MINIMUM_TOKEN_LIMIT = 64
DEFAULT_MAX_CHARACTERS = 15000


def split_markdown_file(md_file, max_characters=0, token_limit=None, gpt_model=None):
    """
    @param md_file: string containing path to markdown file
    @param max_characters: maximum number of characters for each section
    @param token_limit: maximum number of GPT tokens to use when creating sections
    @param gpt_model: GPT model to use when splitting into sections (changes default token limits)
    @return (frontmatter_string, sections): Frontmatter of md file, and rest split into list of sections
    """
    # First make sure we have an actual file
    if not os.path.isfile(md_file):
        raise FileNotFoundError(
            "The markdown file you gave (%s) does not exist!" % md_file
        )
    split_kwargs = {}
    if max_characters != 0 and not token_limit:
        # Need to set an initial token size that we can tweak later
        token_limit = DEFAULT_TOKEN_LIMIT
    if token_limit:
        split_kwargs["limit"] = token_limit
    if gpt_model:
        split_kwargs["model"] = gpt_model
    sections_fit = False
    # Grab the front matter, we're going to need it to reconstruct the document
    frontmatter = Frontmatter.read_file(md_file)
    if frontmatter:
        frontmatter_string = "---" + frontmatter["frontmatter"] + "---\n"
    else:
        frontmatter_string = ""
    while not sections_fit:
        sections = split(Path(md_file), **split_kwargs)
        section_limit_exceeded = False
        for section in sections:
            if 0 < max_characters < len(section):
                section_limit_exceeded = True
                # print("Section limit exceeded: %d characters (Max %d)" % (len(section), max_characters))
                break
        if section_limit_exceeded:
            # Reduce token size (and we know token limit is set for this to trigger)
            split_kwargs["limit"] = split_kwargs["limit"] - MINIMUM_TOKEN_LIMIT
            if split_kwargs["limit"] < MINIMUM_TOKEN_LIMIT:
                raise ArithmeticError(
                    "Minimum token limit %d reached!" % split_kwargs["limit"]
                )
        else:
            sections_fit = True
    print("Number of sections: %d" % len(sections))

    return frontmatter_string, sections


def translate_deepl(
    sections, source_lang="EN", target_lang=None, glossary=None, auth_key=None
):
    if not target_lang:
        raise ValueError(
            "You must provide a valid target language! ('%s' given)" % target_lang
        )
    if not auth_key:
        raise ValueError("You must provide a valid authentication key for DeepL!")
    translator = deepl.Translator(auth_key)

    # Make sure we have enough credits for the full translation
    total_characters = sum([len(section) for section in sections])
    usage = translator.get_usage()
    if usage.any_limit_reached:
        raise RuntimeError("Translation limit reached on DeepL :( ")
    if usage.character.valid:
        if total_characters > usage.character.limit - usage.character.count:
            raise RuntimeError(
                f"Character usage: {usage.character.count} of {usage.character.limit}, need {total_characters} for "
                f"translation!"
            )
    # Configure kwargs for translator
    translator_kwargs = {
        "source_lang": source_lang.upper(),
        "target_lang": target_lang.upper(),
        "preserve_formatting": True,
    }
    if glossary:
        # Create the DeepL glossary from the given dict
        temp_glossary = translator.create_glossary(
            "Temporary glossary",
            source_lang=source_lang.upper(),
            target_lang=target_lang.upper(),
            entries=glossary,
        )
        translator_kwargs["glossary"] = temp_glossary

    results = []
    for section in sections:
        section_translation = translator.translate_text(
            section,
            **translator_kwargs,
        )
        results.append(section_translation.text)
    # Delete the temporary glossary
    if glossary:
        translator.delete_glossary(temp_glossary)
    return results


markdown_file = "index.md"
output_file = "out.md"
auth_key = os.getenv("DEEPL_AUTH_KEY")
if not auth_key:
    raise RuntimeError(
        "Nothing will work without providing an authentication key for your translator API"
    )

# frontmatter, sections = split_markdown_file(markdown_file, max_characters=DEFAULT_MAX_CHARACTERS)
# translated_sections = translate_deepl(sections, target_lang='es', auth_key=auth_key, glossary={":::  solution": ":::  solution"})

# if output_file:
#    md_output_dest = open(output_file, 'w')
# else:
#    md_output_dest = sys.stdout
# print(frontmatter, file=md_output_dest)
# for section in translated_sections:
#    print(section, file=md_output_dest)
frontmatter_string = ""
frontmatter = Frontmatter.read_file(markdown_file)
if frontmatter["frontmatter"]:
    frontmatter_string = "---" + frontmatter["frontmatter"] + "---\n"

with open(markdown_file, "r") as fin:
    # Use a renderer with massive line length for the translation so that we never have line breaks in paragraphs
    with MarkdownRenderer(max_line_length=10000) as final_renderer:
        document = mistletoe.Document(fin)
        # Drop the frontmatter as it doesn't get displayed correctly
        # First drop any blank lines
        while isinstance(document.children[0], BlankLine):
            document.children.pop(0)
        if isinstance(document.children[0], ThematicBreak):
            document.children.pop(0)
        if isinstance(document.children[0], SetextHeading):
            document.children.pop(0)
        translated_document = translate_block(
            document, renderer=final_renderer, target_lang="es", auth_key=auth_key
        )
        # translated_document = document
    # Use a shorter line length for the final rendering
    with MarkdownRenderer(max_line_length=88) as short_renderer:
        md = short_renderer.render(translated_document)
        if frontmatter_string:
            print(frontmatter_string)
        print(md)
