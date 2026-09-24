//! Attach OSC 8 targets to rendered labels for the terminal's native link handling.
use crossterm::{
    cursor::{RestorePosition, SavePosition},
    queue,
};
use ratatui::{
    backend::{Backend, CrosstermBackend},
    buffer::Buffer,
};
use std::{
    collections::BTreeSet,
    io::{self, Write},
};
use unicode_width::UnicodeWidthStr;

pub struct Link {
    pub y: u16,
    pub start: u16,
    pub end: u16,
    pub url: String,
}

#[derive(Default)]
pub struct Writer {
    previous_rows: BTreeSet<u16>,
}
impl Writer {
    pub fn write(
        &mut self,
        buffer: &Buffer,
        links: &[Link],
        out: &mut impl Write,
    ) -> io::Result<()> {
        let current_rows: BTreeSet<_> = links.iter().map(|link| link.y).collect();
        let rows: BTreeSet<_> = self.previous_rows.union(&current_rows).copied().collect();
        if rows.is_empty() {
            return Ok(());
        }
        queue!(out, SavePosition)?;
        // Redraw prior link rows too: Ratatui's cell diff cannot see changed OSC 8 metadata.
        for y in rows
            .into_iter()
            .filter(|y| buffer.area.top() <= *y && *y < buffer.area.bottom())
        {
            let row_links: Vec<_> = links.iter().filter(|link| link.y == y).collect();
            let mut x = buffer.area.left();
            while x < buffer.area.right() {
                let target = |x| {
                    row_links
                        .iter()
                        .find(|link| link.start <= x && x < link.end)
                        .map(|link| link.url.as_str())
                        .unwrap_or("")
                };
                let url = target(x);
                write!(out, "\x1b]8;;{url}\x1b\\")?;
                let mut cells = Vec::new();
                loop {
                    let cell = &buffer[(x, y)];
                    cells.push((x, y, cell));
                    x += cell.symbol().width().max(1) as u16;
                    if x >= buffer.area.right() || target(x) != url {
                        break;
                    }
                }
                CrosstermBackend::new(&mut *out).draw(cells.into_iter())?;
            }
        }
        out.write_all(b"\x1b]8;;\x1b\\")?;
        queue!(out, RestorePosition)?;
        out.flush()?;
        self.previous_rows = current_rows;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{layout::Rect, style::Style};

    #[test]
    fn targets_are_invisible_and_are_cleared_when_links_disappear() {
        let mut buffer = Buffer::empty(Rect::new(0, 0, 20, 2));
        buffer.set_string(0, 0, "PR #16712", Style::default());
        let mut writer = Writer::default();
        let mut bytes = vec![];
        writer
            .write(
                &buffer,
                &[Link {
                    y: 0,
                    start: 3,
                    end: 9,
                    url: "https://example.com/16712".into(),
                }],
                &mut bytes,
            )
            .unwrap();
        let output = String::from_utf8(bytes).unwrap();
        assert!(output.contains("\x1b]8;;https://example.com/16712\x1b\\"));
        assert!(output.contains("#16712"));
        assert!(output.contains("\x1b]8;;\x1b\\"));
        let mut cleared = vec![];
        writer.write(&buffer, &[], &mut cleared).unwrap();
        let cleared = String::from_utf8(cleared).unwrap();
        assert!(cleared.contains("#16712"));
        assert!(!cleared.contains("https://"));
        assert!(cleared.contains("\x1b]8;;\x1b\\"));
        let mut idle = vec![];
        writer.write(&buffer, &[], &mut idle).unwrap();
        assert!(idle.is_empty());
    }
}
