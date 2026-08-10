# TerminalOS

TerminalOS is a Debian-based, terminal-first Linux distribution with a custom kernel, graphical desktop, installer, signed repositories, and streamlined package-management tools.

## Project status

**TerminalOS 1.0.0** was completed and validated on August 6, 2026.

Development of **TerminalOS 1.0.1** is underway and includes `terminalos-package-manager` 1.1.0.

## Highlights

- Debian-based operating system
- Custom Linux `6.12.94-terminalos` kernel
- Boots on legacy BIOS and UEFI systems
- Graphical desktop and guided installer
- Signed TerminalOS software repositories
- Terminal-focused system utilities
- `tos` package-management interface
- Verified release image

## Package management

The `tos` command provides a streamlined interface for common package and repository tasks, including:

- Package search and information
- Installed-package inspection
- File ownership queries
- Repository management
- Transaction history
- System diagnostics and repair

## TerminalOS 1.0.0 verification

Release image:

```text
TerminalOS-1.0.0.iso
```

SHA-256:

```text
0961b2bacb52cf4cbe5dd8c370d9e267c5f49decd05840ccff818b4cbaaf99c6
```

Verify a downloaded image on Linux:

```bash
sha256sum TerminalOS-1.0.0.iso
```

The result must exactly match the checksum above.

## Repository status

The public repository is being initialized. Source code, package sources, build tooling, documentation, and downloadable releases will be added as they are prepared for publication.

## Support and development

Use GitHub Issues to report reproducible bugs or request features. Include the TerminalOS version, hardware or virtual-machine configuration, and exact commands or error output when reporting a problem.
