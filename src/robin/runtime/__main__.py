"""Entry point for runtime hosts: `python -m robin.runtime --runtime NAME --cwd DIR`.

Hosts must not run as `-m robin.runtime.host`: the module would load twice (as __main__ and as
robin.runtime.host), and adapters would register into the copy that main() doesn't read.
"""

from robin.runtime.host import main

main()
