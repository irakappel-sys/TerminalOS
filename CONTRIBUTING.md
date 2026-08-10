# Contributing to TerminalOS

TerminalOS welcomes focused bug reports, documentation improvements, and tested code contributions.

## Reporting bugs

Open a GitHub Issue and include:

- TerminalOS version
- Physical hardware or virtual-machine configuration
- Whether the system booted through BIOS or UEFI
- Exact steps needed to reproduce the problem
- Complete commands and error output
- Relevant logs, with passwords, tokens, and private information removed

## Proposing changes

1. Open an issue describing substantial changes before implementing them.
2. Keep each pull request limited to one logical change.
3. Explain what changed, why it changed, and how it was tested.
4. Update documentation when behavior or commands change.
5. Do not commit generated ISO images, virtual disks, credentials, or machine-specific files.

## Testing

Changes affecting boot, installation, packaging, or the kernel should be tested in a clean virtual machine. Boot-related changes should cover both legacy BIOS and UEFI whenever possible.

## Commit messages

Use short, direct commit subjects written as commands, for example:

```text
Fix UEFI installer boot entry
Add repository signature verification
Document tos repair command
```
