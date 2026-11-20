"""Small Bash transport adapter for Typer's built-in install/show flags.

Typer flattens COMP_WORDS, losing spaces inside arguments; Bash also splits
URLs and HOST=PATH at word breaks. Pass the original command line instead.
Keep the adapter here because Typer 0.25 uses Click and 0.29 vendors it; neither
our command callbacks nor this adapter should import external Click.
"""
import os

_SCRIPT = r'''
%(complete_func)s() {
    local candidate
    local LC_ALL=C
    COMPREPLY=()
    while IFS= read -r candidate; do
        [[ -n "$candidate" ]] && COMPREPLY+=("$candidate")
    done < <(env _MMW_BASH_LINE="${COMP_LINE:0:COMP_POINT}" \
                _MMW_BASH_WORD="${COMP_WORDS[COMP_CWORD]}" \
                _MMW_BASH_BREAKS="$COMP_WORDBREAKS" \
                %(autocomplete_var)s=complete_bash "$1")
    # Let Readline escape spaces and shell metacharacters, and keep walking
    # directories / HOST= mappings without inserting a space after them.
    compopt -o filenames 2>/dev/null || true
    if [[ ${#COMPREPLY[@]} == 1 && ${COMPREPLY[0]} == *[/=] ]]; then
        compopt -o nospace 2>/dev/null || true
    fi
    return 0
}

complete -F %(complete_func)s %(prog_name)s
'''


def _line_args(line, split):
    words = split(line)
    quote, escaped, separated = None, False, False
    for char in line:
        if escaped:
            escaped, separated = False, False
        elif char == '\\' and quote != "'":
            escaped, separated = True, False
        elif quote:
            if char == quote:
                quote = None
            separated = False
        elif char in ('"', "'"):
            quote, separated = char, False
        else:
            separated = char.isspace()
    if separated:
        return words[1:], ''
    return words[1:-1], words[-1] if len(words) > 1 else ''


def configure_bash():
    # Isolated compatibility seam: both supported Typer layouts expose these
    # hooks. Installation and --show-completion must emit the same script.
    from typer import _completion_classes as classes, _completion_shared as shared
    if getattr(classes.BashComplete, '_mmw_transport', False):
        return
    original_args = classes.BashComplete.get_completion_args
    original_format = classes.BashComplete.format_completion

    def args(self):
        if '_MMW_BASH_LINE' in os.environ:
            return _line_args(os.environ['_MMW_BASH_LINE'], classes.click_split_arg_string)
        return original_args(self)

    def format_item(self, item):
        value = original_format(self, item)
        if '_MMW_BASH_LINE' not in os.environ:
            return value
        _, incomplete = self.get_completion_args()
        current = os.environ.get('_MMW_BASH_WORD', '')
        # Readline replaces only the part following an unquoted word break.
        if current != incomplete:
            breaks = os.environ.get('_MMW_BASH_BREAKS', '')
            index = max((incomplete.rfind(c) for c in ':=' if c in breaks), default=-1)
            prefix = incomplete[:index + 1]
            if prefix and value.startswith(prefix):
                value = value[len(prefix):]
        return value

    classes.BashComplete.get_completion_args = args
    classes.BashComplete.format_completion = format_item
    classes.BashComplete.source_template = _SCRIPT
    classes.BashComplete._mmw_transport = True
    shared._completion_scripts['bash'] = _SCRIPT
