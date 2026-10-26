"""Hidden terminal entry for server-side provider keys. Never prints keys."""
from __future__ import annotations

import getpass
import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from .data import ROOT

KEY_PATH = ROOT / '.local' / 'openai.json'
GEMINI_KEY_PATH = ROOT / '.local' / 'gemini.json'


def provider_path(provider):
    if provider not in ('openai', 'gemini'):
        raise ValueError('Choose gemini or openai')
    return GEMINI_KEY_PATH if provider == 'gemini' else KEY_PATH


def key_format(key, provider):
    return (isinstance(key, str) and 20 <= len(key) <= 512 and key.isascii()
            and not any(c.isspace() for c in key)
            and (key.startswith('sk-') if provider == 'openai' else not key.startswith('sk-')))


def save_key(key, path=None, *, provider='openai'):
    path = Path(path) if path is not None else provider_path(provider)
    # Hidden prompts give no visual paste feedback. Repair only an exact repeat
    # of a complete key, never guess at a truncated or different credential.
    if isinstance(key, str):
        for copies in range(8, 1, -1):
            if len(key) % copies == 0:
                candidate = key[:len(key) // copies]
                if key_format(candidate, provider) and candidate * copies == key:
                    key = candidate
                    break
    if not key_format(key, provider):
        name = 'Gemini' if provider == 'gemini' else 'OpenAI'
        raise ValueError('That does not look like a ' + name + ' key. Copy the full key from its dashboard.')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.openai-', delete=False) as file:
            temporary = Path(file.name)
            os.chmod(temporary, 0o600)
            json.dump({'api_key': key}, file)
            file.write('\n')
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def load_key(path=None, *, provider='openai'):
    """Environment takes precedence. Ignore unreadable/insecure local files."""
    path = Path(path) if path is not None else provider_path(provider)
    value = (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY', '')).strip() if provider == 'gemini' else os.environ.get('OPENAI_API_KEY', '').strip()
    if value:
        return value
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor) as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                return ''
            if hasattr(os, 'getuid') and info.st_uid != os.getuid():
                return ''
            record = json.load(file)
        value = record.get('api_key', '')
        return value if isinstance(value, str) else ''
    except (OSError, ValueError, AttributeError):
        return ''


def main():
    parser = argparse.ArgumentParser(description='Save an LLM key using a hidden terminal prompt')
    parser.add_argument('--provider', choices=('gemini', 'openai'), default='gemini')
    args = parser.parse_args()
    name = 'Gemini' if args.provider == 'gemini' else 'OpenAI'
    if not sys.stdin.isatty():
        print('Open Configure ' + name + '.command in Terminal to enter the key privately.', file=sys.stderr)
        return 1
    print('Connect ' + name + ' to F1 Pitwall\n')
    if args.provider == 'gemini':
        print('Use a Google AI Studio project marked Free tier. Do not enable billing for this demo.\n')
    print('Paste the API key at the hidden prompt, then press Return.')
    print('No characters will appear while you paste. The key stays on this computer,')
    print('in an owner-only file ignored by Git. It is never sent to chat.\n')
    try:
        key = getpass.getpass(name + ' API key (hidden): ').strip()
        save_key(key, provider=args.provider)
        print('\nKey saved. Return to Codex and reply “saved” so the app can be restarted and tested.')
        try:
            input('\nPress Return to close this setup window. ')
        except (EOFError, KeyboardInterrupt):
            pass
        return 0
    except (EOFError, KeyboardInterrupt):
        print('\nSetup canceled. Existing configuration is unchanged.')
        return 1
    except ValueError as error:
        print('\n' + str(error), file=sys.stderr)
        input('\nPress Return, then reopen setup to try again. ')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
