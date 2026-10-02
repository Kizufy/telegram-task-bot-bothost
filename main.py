"""Entry point for hosts that start a Python file without CLI arguments."""
import runpy
import sys

if __name__ == '__main__':
    if len(sys.argv) == 1:
        sys.argv.append('run')
    runpy.run_module('bot', run_name='__main__')
