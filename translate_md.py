import os
import re
import sys
import glob
import csv
import deepl
import mistletoe
import pathlib
import frontmatter
import click
from mistletoe.block_token import (
    BlockToken,
    Heading,
    Paragraph,
    SetextHeading,
    ThematicBreak,
)
from mistletoe.markdown_renderer import MarkdownRenderer, BlankLine
from mistletoe.span_token import InlineCode, RawText, SpanToken, Image

# Define the max line length for our MarkdownRenderer to ensure paragraphs are single lines
MAX_LINE_LENGTH = 10000
# Define desired output line length (Black uses 88)
OUTPUT_LINE_LENGTH = 88
# Give some DeepL information
DEEPL_SOURCE_LANGUAGES = [
    ("BG", "Bulgarian"),
    ("CS", "Czech"),
    ("DA", "Danish"),
    ("DE", "German"),
    ("EL", "Greek"),
    ("EN", "English"),
    ("ES", "Spanish"),
    ("ET", "Estonian"),
    ("FI", "Finnish"),
    ("FR", "French"),
    ("HU", "Hungarian"),
    ("ID", "Indonesian"),
    ("IT", "Italian"),
    ("JA", "Japanese"),
    ("KO", "Korean"),
    ("LT", "Lithuanian"),
    ("LV", "Latvian"),
    ("NB", "Norwegian (Bokmål)"),
    ("NL", "Dutch"),
    ("PL", "Polish"),
    ("PT", "Portuguese (all Portuguese varieties mixed)"),
    ("RO", "Romanian"),
    ("RU", "Russian"),
    ("SK", "Slovak"),
    ("SL", "Slovenian"),
    ("SV", "Swedish"),
    ("TR", "Turkish"),
    ("UK", "Ukrainian"),
    ("ZH", "Chinese"),
]
DEEPL_TARGET_LANGUAGES = DEEPL_SOURCE_LANGUAGES
DEEPL_GLOSSARY_LANGUAGES = [
    "DE",
    "EN",
    "ES",
    "FR",
    "IT",
    "JA",
    "NL",
    "PL",
    "PT",
    "RU",
    "ZH",
]
# Request body size limit of 128 KiB, which is 16K characters
# (but we also need to leave room for any other arguments)
DEEPL_MAX_REQUEST_CHARACTERS = 12000
MAX_TRANSLATION_CONTEXT = DEEPL_MAX_REQUEST_CHARACTERS
# Define accepted markdown formats
ACCEPTED_MARKDOWN_FILE_EXTENSIONS = [
    ".md",
    ".rmd",
    ".mkd",
    ".mdwn",
    ".mdown",
    ".mdtxt",
    ".mdtext",
    ".markdown",
    ".text",
]


def replace_inline_code(token: SpanToken, inline_code_dict: dict):
    """Update the text contents of a span token and its children.
    `InlineCode` tokens are left unchanged."""
    if isinstance(token, InlineCode):
        # Generate a  placeholder
        placeholder = "x%03dy" % len(inline_code_dict)
        # Add the placeholder to the dict
        inline_code_dict[placeholder] = token.children[0].content
        token.children[0].content = placeholder

    if hasattr(token, "children") and not isinstance(token, InlineCode) and token.children is not None:
        for child in token.children:
            replace_inline_code(child, inline_code_dict)


def restore_inline_code(token: SpanToken, inline_code_dict: dict):
    """Update the text contents of a span token and its children.
    `InlineCode` tokens are left unchanged."""
    if isinstance(token, InlineCode):
        if token.children[0].content in inline_code_dict.keys():
            token.children[0].content = inline_code_dict.pop(token.children[0].content)

    if hasattr(token, "children") and not isinstance(token, InlineCode) and token.children is not None:
        for child in token.children:
            replace_inline_code(child, inline_code_dict)


def translate_block(
    token: BlockToken,
    renderer=None,
    ignore_triple_colon=True,
    source_lang="EN",
    target_lang=None,
    glossary={},
    auth_key=None,
    char_count_only=True,
    translation_context=None,
):
    """Update the text contents of paragraphs and headings within this block,
    and recursively within its children."""

    # First check some input arguments
    check_typical_arguments(
        source_lang=source_lang,
        target_lang=target_lang,
        glossary=glossary,
        auth_key=auth_key,
        char_count_only=char_count_only,
    )

    # We are only translating selected elements of the markdown
    allowed_blocks = (Paragraph, Heading)

    # Create a markdown renderer if we don't have one already
    if renderer is None:
        renderer = MarkdownRenderer(max_line_length=MAX_LINE_LENGTH)

    # By default, just assume we return what we got and a char_count of 0
    translated_token = token
    char_count = 0

    # if isinstance(token, (Paragraph, SetextHeading, Heading)):
    if isinstance(token, allowed_blocks):
        # Ignore any paragraph that starts with ':::' (this is Carpentries Workbench specific)
        if (
            ignore_triple_colon
            and isinstance(token, Paragraph)
            and isinstance(token.children[0], RawText)
            and token.children[0].content.startswith(":::")
        ):
            pass
        else:
            # If we have the alt text that is plain html and sits beside an image
            # then let's chop the tags off and reinsert them later
            add_alt = False
            if (
                isinstance(token, Paragraph)
                and isinstance(token.children[0], Image)
                and isinstance(token.children[1], RawText)
                and token.children[1].content.startswith("{alt='")
                and isinstance(token.children[-1], RawText)
                and token.children[-1].content.endswith("'}")
            ):
                add_alt =True
                token.children[1].content = token.children[1].content.replace("{alt='", "")
                token.children[-1].content = token.children[-1].content.replace("'}", "")
            # Replace all the inline code blocks with placeholders
            inline_code_dict = {}
            for child in token.children:
                replace_inline_code(child, inline_code_dict)
            # Reconstruct the resulting markdown block to give a full context to translate
            markdown_text = renderer.render(token)

            # Translate the block using a specific machine translator
            char_count, translated_markdown = translate_block_deepl(
                markdown_text,
                source_lang=source_lang,
                target_lang=target_lang,
                glossary=glossary,
                auth_key=auth_key,
                char_count_only=char_count_only,
                translation_context=translation_context,
            )

            # Ensure inline code syntax is preserved
            # (some failures are allowed, so we also may be updating the dictionary)
            translated_markdown, inline_code_dict = ensure_inline_code_syntax(
                translated_markdown, inline_code_dict=inline_code_dict
            )

            # Deconstruct the resulting markdown again and identify the token we need
            temp_document = mistletoe.Document(translated_markdown)

            for child in temp_document.children:
                # Assuming here that first paragraph is a hit
                if isinstance(child, allowed_blocks):
                    translated_token = child
                    break
            if translated_token is None:
                raise RuntimeError(
                    "Something went wrong, we didn't get translation token back: \n%s"
                    % renderer.render(temp_document)
                )
            # Replace all the placeholders with their inline codeblocks
            for child in translated_token.children:
                restore_inline_code(child, inline_code_dict)
            if len(inline_code_dict):
                print(markdown_text)
                print(translated_markdown)
                raise RuntimeError(
                    "Something went wrong, you should have an empty dict after translation but you have: %s"
                    % inline_code_dict
                )
            if add_alt:
                # Add back our alt text
                translated_token.children[1].content = "{alt='" + translated_token.children[1].content
                translated_token.children[-1].content = translated_token.children[-1].content + "'}"

    if hasattr(token, "children") and token.children is not None:
        for index, child in enumerate(token.children):
            if isinstance(child, BlockToken):
                child_char_count, token.children[index] = translate_block(
                    child,
                    renderer=renderer,
                    ignore_triple_colon=ignore_triple_colon,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    glossary=glossary,
                    auth_key=auth_key,
                    char_count_only=char_count_only,
                    translation_context=translation_context,
                )
                char_count += child_char_count

    return char_count, translated_token


def check_typical_arguments(
    auth_key=None, char_count_only=True, glossary={}, source_lang="EN", target_lang=None
):
    if not source_lang or not (isinstance(source_lang, str) and len(source_lang) == 2):
        raise ValueError(
            "You gave %s as a source language must be a two letter language code from: %s"
            % (source_lang, DEEPL_SOURCE_LANGUAGES)
        )
    if not target_lang or not (isinstance(target_lang, str) and len(target_lang) == 2):
        raise ValueError(
            "You gave %s as a target language must be a two letter language code from: %s"
            % (target_lang, DEEPL_TARGET_LANGUAGES)
        )
    # Accepted languages
    accepted_source_langs = [lang[0] for lang in DEEPL_SOURCE_LANGUAGES]
    accepted_target_langs = [lang[0] for lang in DEEPL_TARGET_LANGUAGES]
    if source_lang.upper() not in accepted_source_langs:
        raise ValueError(
            "Source language %s is not in the accepted options: %s"
            % (source_lang, accepted_source_langs)
        )
    if target_lang.upper() not in accepted_target_langs:
        raise ValueError(
            "Source language %s is not in the accepted options: %s"
            % (target_lang, accepted_target_langs)
        )
    if source_lang.upper() == target_lang.upper():
        raise ValueError(
            "Your source and target languages are the same! (%s and %s)"
            % (source_lang, target_lang)
        )

    if not isinstance(glossary, dict):
        raise ValueError(
            "Glossary is given as a dict with source language vocab as keys and target "
            "language vocab as values, you gave: %s" % target_lang
        )

    if glossary:
        if (
            source_lang.upper() not in DEEPL_GLOSSARY_LANGUAGES
            or target_lang.upper() not in DEEPL_GLOSSARY_LANGUAGES
        ):
            raise ValueError(
                "Glossaries only work between certain languages: %s"
                % DEEPL_GLOSSARY_LANGUAGES
            )

    check_auth_key(auth_key, char_count_only, error_only=True)


def check_auth_key(auth_key, char_count_only=True, error_only=False):
    if not auth_key:
        msg = "No authentication token given, so can't make translation or query translation API"
        if char_count_only:
            if not error_only:
                print(msg)
        else:
            raise ValueError(msg)


def ensure_inline_code_syntax(translated_markdown, inline_code_dict={}):
    # Make sure all our dict elements exists and are surrounded by `
    # (a machine translator may decide to change them)
    missed_keys = 0
    keys_to_pop=[]
    for key in inline_code_dict.keys():
        # weird things with case can happen, let's fix that already
        insensitive_key = re.compile(re.escape(key), re.IGNORECASE)
        translated_markdown = insensitive_key.sub(key, translated_markdown)
        if key not in translated_markdown:
            # Let's be a little forgiving here and raise a warning
            # but if it happens more than twice, make it an error
            msg = "Code placeholder %s (value %s) does not appear in translation:\n%s" % (key, inline_code_dict[key], translated_markdown)
            print("Warning %d:\n%s" % (missed_keys, msg))
            if missed_keys < 2:
                keys_to_pop.append(key)
                continue
            else:
                raise RuntimeError("Too many warnings for missing code placeholders, exiting!")
        location = translated_markdown.find(key)
        translated_markdown = (
            translated_markdown[: location - 1]
            + "`"
            + key
            + "`"
            + translated_markdown[location + len(key) + 1 :]
        )
    # If we allowed some key misses, pop them from the dictionary
    for key in keys_to_pop:
        inline_code_dict.pop(key)
    return translated_markdown, inline_code_dict


def translate_block_deepl(
    markdown_text,
    source_lang="EN",
    target_lang=None,
    auth_key=None,
    glossary={},
    char_count_only=False,
    translation_context=None,
):
    check_typical_arguments(
        source_lang=source_lang,
        target_lang=target_lang,
        glossary=glossary,
        auth_key=auth_key,
        char_count_only=char_count_only,
    )
    if len(markdown_text) == 0:
        return markdown_text

    # Starting or ending with special markdown syntax seems to cause syntax loss, so let's work around that by
    # adding something that can't get translated
    # - leaving a '.' at the end can sometimes cause DeepL to remove the subsequent space
    # - Left a space as you don't want to mess with the first/last word
    start_marker = "XYZ.1: "
    end_marker = "".join(reversed(start_marker))
    markdown_text_to_translate = start_marker + markdown_text + end_marker

    # Translate the resulting markdown text
    char_count = len(markdown_text_to_translate)
    if char_count_only:
        translated_markdown = markdown_text_to_translate
    else:
        translated_markdown = translate_deepl(
            markdown_text_to_translate,
            source_lang=source_lang,
            target_lang=target_lang,
            glossary=glossary,
            auth_key=auth_key,
            translation_context=translation_context,
        )
    translated_markdown = translated_markdown.strip()

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

    return char_count, translated_markdown


def translate_deepl(
    text, source_lang="EN", target_lang=None, glossary=None, auth_key=None, translation_context=None
):
    check_typical_arguments(
        source_lang=source_lang,
        target_lang=target_lang,
        glossary=glossary,
        auth_key=auth_key,
        char_count_only=False,
    )

    if not auth_key:
        raise ValueError("You must provide a valid authentication key to use DeepL!")
    translator = deepl.Translator(auth_key)

    # Make sure we have enough credits for the full translation
    total_characters = len(text)
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

    # Add translation context if we have some
    if translation_context:
        # There's a limit on how big a request can be but let's just give as much context as possible
        characters_to_send = min((MAX_TRANSLATION_CONTEXT - total_characters), len(translation_context))
        print("Sending context: %s", translation_context[:characters_to_send])
        translator_kwargs["context"] = translation_context[:characters_to_send]
        print(translation_context[:characters_to_send])

    result = translator.translate_text(text, **translator_kwargs).text

    # Delete the temporary glossary
    if glossary:
        translator.delete_glossary(temp_glossary)

    return result


def avail_char_quota_deepl(auth_key=None):
    # Check available character quota
    translator = deepl.Translator(auth_key)
    available_characters = -1
    if auth_key:
        usage = translator.get_usage()
        available_characters = usage.character.limit - usage.character.count
    else:
        print("No auth_key provided, returning -1 for available character quota")

    return available_characters


def extract_frontmatter_dict(markdown_file):
   return frontmatter.load(markdown_file).metadata

def create_frontmatter_string(frontmatter_dict):
    frontmatter_string = ""
    if frontmatter_dict:
        frontmatter_string = (
            "---\n"
            + "\n".join(
                "%s: %s" % (key, frontmatter_dict[key]) for key in frontmatter_dict.keys()
            )
            + "\n---\n"
        )

    return frontmatter_string


def translate_markdown_file(
    markdown_file,
    output_file=None,
    source_lang="EN",
    target_lang=None,
    glossary={},
    auth_key=None,
    char_count_only=True,
    ignore_triple_colon=True,
):
    check_typical_arguments(
        source_lang=source_lang,
        target_lang=target_lang,
        glossary=glossary,
        auth_key=auth_key,
        char_count_only=char_count_only,
    )
    # Make sure the file exists and has a recognised extension
    if not os.path.isfile(markdown_file):
        raise ValueError("Input markdown file %s does not exist!" % markdown_file)
    md_extension = pathlib.Path(markdown_file).suffix
    if md_extension.lower() not in ACCEPTED_MARKDOWN_FILE_EXTENSIONS:
        print(md_extension.lower())
        raise ValueError(
            "File %s does not have an extension in the accepted list (): %s"
            % (markdown_file, ACCEPTED_MARKDOWN_FILE_EXTENSIONS)
        )

    # Extract the front matter
    frontmatter_dict = extract_frontmatter_dict(markdown_file)

    with open(markdown_file, "r") as fin:
        # Let's get some translation context
        translation_context = fin.read(MAX_TRANSLATION_CONTEXT)
        fin.seek(0)
        # Use a renderer with massive line length for the translation so that we never have line breaks in paragraphs
        with MarkdownRenderer(max_line_length=MAX_LINE_LENGTH) as renderer:
            document = mistletoe.Document(fin)
            # Drop the frontmatter as it doesn't get displayed correctly
            # First drop any blank lines
            while isinstance(document.children[0], BlankLine):
                document.children.pop(0)
            if isinstance(document.children[0], ThematicBreak):
                document.children.pop(0)
            if isinstance(document.children[0], SetextHeading):
                document.children.pop(0)
            char_count, translated_document = translate_block(
                document,
                renderer=renderer,
                source_lang=source_lang,
                target_lang=target_lang,
                glossary=glossary,
                auth_key=auth_key,
                char_count_only=char_count_only,
                ignore_triple_colon=ignore_triple_colon,
                translation_context=translation_context,
            )
    # Also translate the title if it exists
    if "title" in frontmatter_dict:
        if char_count_only:
            char_count += len(frontmatter_dict["title"])
        else:
            frontmatter_dict["title"] = translate_deepl(
                frontmatter_dict["title"],
                source_lang=source_lang,
                target_lang=target_lang,
                glossary=glossary,
                auth_key=auth_key)
    if not char_count_only or output_file:

        if output_file:
            # Let's work with full paths
            output_file_path = os.path.abspath(output_file)
            try:
                os.makedirs(os.path.dirname(output_file_path))
            except FileExistsError:
                pass
            md_output_dest = open(output_file_path, "w")
        else:
            md_output_dest = sys.stdout

        # Use a shorter line length for the final rendering
        with MarkdownRenderer(max_line_length=OUTPUT_LINE_LENGTH) as short_renderer:
            md = short_renderer.render(translated_document)
            if frontmatter_dict:
                print(create_frontmatter_string(frontmatter_dict), file=md_output_dest)
            print(md, file=md_output_dest)

    return char_count


@click.command()
@click.option(
    "--input-markdown-filestring",
    required=True,
    help='Can be a single file ("index.md"), or a string with wildcards '
    "(\"'*/*.md'\") to match files",
    type=str,
)
@click.option(
    "--source-lang",
    default="en",
    help="Original language of the markdown file(s)",
    type=click.Choice(
        [short_code.lower() for short_code, language in DEEPL_SOURCE_LANGUAGES],
        case_sensitive=False,
    ),
    show_default=True,
)
@click.option(
    "--target-lang",
    required=True,
    help="Target language for the markdown file(s)",
    type=click.Choice(
        [short_code.lower() for short_code, language in DEEPL_TARGET_LANGUAGES],
        case_sensitive=False,
    ),
)
@click.option(
    "--output-subdir",
    is_flag=True,
    default=False,
    help="Flag to indicate if translated documents should be placed in a subdirectory",
    show_default=True,
)
@click.option(
    "--output-suffix",
    is_flag=True,
    default=False,
    help="Flag to indicate if translated documents should written in the same location"
    " with a translation suffix",
    show_default=True,
)
@click.option(
    "--output-suffix-char",
    default="_",
    help="Character to use to separate the filename and translation suffix",
    type=str,
    show_default=True,
)
@click.option(
    "--char-count-only",
    is_flag=True,
    default=False,
    help="Flag to indicate if we should just count characters that would be translated",
    show_default=True,
)
@click.option(
    "--glossary-file",
    help="CSV file that contains glossary terms to be used in the translation (no headers, "
    "just two columns with source language term then target language translation)",
    type=str,
)
@click.option(
    "--authentication-key", help="Authentication key for translation API", type=str
)
def translate_markdown_files(
    input_markdown_filestring,
    source_lang="EN",
    target_lang=None,
    output_subdir=False,
    output_suffix=False,
    output_suffix_char="_",
    char_count_only=True,
    glossary_file=None,
    authentication_key=None,
):
    # Check our authentication key
    check_auth_key(authentication_key, error_only=False)

    # First gather our file list
    markdown_files = glob.glob(input_markdown_filestring)
    if not markdown_files:
        raise ValueError(
            "Your markdown matching string ('%s') did not match any files accessible from the current directory."
            % input_markdown_filestring
        )

    # TODO: Be clever with git
    # - Check if we're in git repo
    # - Retain a hidden json file in the repo that contains markdown_file (as key), outputFile and
    #   commit (within dict) from which they were created
    # - read the json and check if the file has been touched since the commit
    #   (if not, we don't need to do anything)
    # - filter the list of files to translate accordingly
    # - if we do translate, update the json with the new info

    if glossary_file:
        # Need to turn our csv glossary file into a dict with source lang term as key
        # and target lang term as value
        with open(glossary_file, mode="r") as infile:
            reader = csv.reader(infile)
            glossary = {rows[0]: rows[1] for rows in reader}
    else:
        glossary = {}
    # Not let's do the work
    total_characters_required = 0
    total_characters_used = 0
    for markdown_file in markdown_files:
        # Construct the output file name/location
        if output_subdir and output_suffix:
            raise ValueError(
                "You must chose between setting a subdirectory for output "
                "('output_subdir', resulting in 'path/to/example/es/example.md') or "
                "adding a suffix to the original file name "
                "('output_suffix', resulting in 'path/to/example/example_es.md')"
            )
        if output_subdir:
            split_path = os.path.split(markdown_file)
            output_file = os.path.join(split_path[0], target_lang, split_path[1])
        elif output_suffix:
            # Split on the extension this time
            split_path = os.path.splitext(markdown_file)
            output_file = (
                split_path[0]
                + "%s%s" % (output_suffix_char, target_lang)
                + split_path[1]
            )
        else:
            output_file = None
        if authentication_key:
            pre_avail_quota = avail_char_quota_deepl(auth_key=authentication_key)
        else:
            pre_avail_quota = -1
        char_count = translate_markdown_file(
            markdown_file,
            output_file=output_file,
            source_lang=source_lang,
            target_lang=target_lang,
            glossary=glossary,
            auth_key=authentication_key,
            char_count_only=char_count_only,
        )
        total_characters_required += char_count
        if authentication_key:
            post_avail_quota = avail_char_quota_deepl(auth_key=authentication_key)
        else:
            post_avail_quota = -1
        if char_count_only:
            if pre_avail_quota == -1:
                quota = "Unknown"
            else:
                quota = "%d" % pre_avail_quota
            print(
                "%s: Translation would use %d characters, available quota is %s"
                % (markdown_file, char_count, quota)
            )
            if 0 < pre_avail_quota < char_count:
                print(
                    "You would not have enough quota to carry out this translation!",
                    file=sys.stderr,
                )
        else:
            actual_quota_usage = pre_avail_quota - post_avail_quota
            total_characters_used += actual_quota_usage
            print(
                "%s: Translation used %d characters, you have %d quota remaining"
                % (markdown_file, actual_quota_usage, post_avail_quota)
            )
            if actual_quota_usage > int(1.1 * char_count):
                print(
                    "Expected quota usage (%d) larger than estimated (%d)! "
                    % (actual_quota_usage, char_count),
                    file=sys.stderr,
                )
    if len(markdown_files) > 1:
        print(
            "Total characters required for translation: %d" % total_characters_required
        )
        if total_characters_used:
            print("Total characters used: %d" % total_characters_used)


if __name__ == "__main__":
    translate_markdown_files()
