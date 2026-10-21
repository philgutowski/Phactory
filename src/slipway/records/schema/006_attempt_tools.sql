/*
 * 006: the tools a builder session was granted (U27).
 *
 * tools_granted lists the project's builder_tools levels the session had,
 * such as {headless_browser, desktop}, so every attempt that could drive
 * the operator's own browser or screen says so on its row. NULL on a row
 * written before this file; an empty array means the shell alone.
 */

ALTER TABLE attempts ADD COLUMN tools_granted text[];
