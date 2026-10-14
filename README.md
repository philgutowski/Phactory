# Phactory

A software factory. Agents take tasks, do the work in isolated environments, and every run is
recorded and graded so the setup keeps improving.

Phactory is project agnostic. It runs against any repository with any tracker on any host,
including ones that do not exist yet and ones that belong to someone else. It carries no
knowledge of any one project; everything project specific is configuration a project supplies,
never code in the tool. That is the first law, and the test every change passes before any other.

Status: seeded 2026-10-14. Nothing is built. The design starts at
`docs/ideation/2026-10-14-software-factory.md`.
