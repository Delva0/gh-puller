//! The CLI supports observation and deterministic offline replay without a model call.
use agent_tui::{
    cli::Observation,
    hyperlinks,
    model::{Event as AgentEvent, Model},
    observer::Observer,
    ui::App,
    workspace::Workspace,
};
use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture},
    execute,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::json;
use std::{
    env,
    fs::File,
    io::{self, BufRead, BufReader, Write},
    path::PathBuf,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args().skip(1);
    let Some(first) = args.next() else {
        help();
        return Ok(());
    };
    if first == "--help" || first == "-h" {
        help();
        return Ok(());
    }
    if first == "--workspace-snapshot" {
        let output = args.next().ok_or("missing snapshot output path")?;
        let files: Vec<_> = args.collect();
        if files.is_empty() {
            return Err("missing snapshot input files".into());
        }
        let mut app = Workspace::new();
        for file in files {
            let mut model = Model::new();
            for line in BufReader::new(File::open(&file)?).lines() {
                model.apply(serde_json::from_str(&line?)?);
            }
            let id = app.add_file(PathBuf::from(file));
            app.apply(id, model.change());
            app.views[id].follow = false;
            app.views[id].scroll = 0;
        }
        let mut terminal = Terminal::new(TestBackend::new(160, 54))?;
        terminal.draw(|f| app.render(f))?;
        for id in 1..app.views.len().min(4) {
            if id == 3 {
                app.command("focus-left", None);
            }
            app.command(if id == 1 { "split-right" } else { "split-down" }, None);
            app.show_file(id);
            terminal.draw(|f| app.render(f))?;
        }
        for _ in 0..200 {
            app.poll_layout();
            terminal.draw(|f| app.render(f))?;
            std::thread::sleep(Duration::from_millis(5));
        }
        let cells: Vec<_> = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| json!({"text":c.symbol(), "fg":color(c.fg), "bg":color(c.bg)}))
            .collect();
        std::fs::write(
            output,
            serde_json::to_vec(&json!({"width":160,"height":54,"cells":cells}))?,
        )?;
        return Ok(());
    }
    if first == "--fold" || first == "--inspect" || first == "--prefixes" || first == "--snapshot" {
        let file = args.next().ok_or("missing JSONL path")?;
        let mut model = Model::new();
        for line in BufReader::new(File::open(&file)?).lines() {
            let event: AgentEvent = serde_json::from_str(&line?)?;
            model.apply(event);
            if first == "--prefixes" {
                println!("{}", model.folded());
            }
        }
        if first == "--prefixes" {
            return Ok(());
        }
        if first == "--fold" {
            println!("{}", model.folded());
            return Ok(());
        }
        if first == "--inspect" {
            model.change();
            println!(
                "{}",
                json!({"state":model.folded(), "cards": model.order.iter().map(|id| &model.cards[id]).collect::<Vec<_>>(), "footer":model.summary.footer, "status":model.summary.status})
            );
            return Ok(());
        }
        let output = args.next().ok_or("missing snapshot output path")?;
        let mut app = App::new(file);
        app.apply(model.change());
        app.follow = false;
        app.scroll = 0;
        let mut terminal = Terminal::new(TestBackend::new(120, 42))?;
        for _ in 0..200 {
            app.poll_layout();
            terminal.draw(|f| app.render(f))?;
            std::thread::sleep(Duration::from_millis(5));
        }
        let buffer = terminal.backend().buffer();
        let cells: Vec<_> = buffer
            .content
            .iter()
            .map(|c| json!({"text":c.symbol(), "fg":color(c.fg), "bg":color(c.bg)}))
            .collect();
        std::fs::write(
            output,
            serde_json::to_vec(&json!({"width":120,"height":42,"cells":cells}))?,
        )?;
        return Ok(());
    }
    let options = Observation::parse(std::iter::once(first).chain(args))?;
    let mut trace = options.trace.map(File::create).transpose()?;
    let mut app = Observer::new(options.sources)?;
    let mut links = hyperlinks::Writer::default();
    let mut terminal = ratatui::init();
    execute!(io::stdout(), EnableMouseCapture)?;
    let original_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let _ = execute!(io::stdout(), DisableMouseCapture);
        ratatui::restore();
        original_hook(info);
    }));
    let result = (|| -> io::Result<()> {
        let mut last_frame = Instant::now() - Duration::from_secs(1);
        let mut first_input: Option<Instant> = None;
        let mut input_count = 0;
        while !app.quit() {
            // Service input before draining worker results; neither parser can hold this thread.
            for _ in 0..64 {
                if !event::poll(Duration::ZERO)? {
                    break;
                }
                let received = Instant::now();
                let event = event::read()?;
                first_input.get_or_insert(received);
                input_count += 1;
                app.handle(event);
            }
            app.poll();
            if app.dirty() && last_frame.elapsed() >= Duration::from_micros(16667) {
                let frame = terminal.draw(|f| app.render(f))?;
                links.write(frame.buffer, &app.hyperlinks(), &mut io::stdout())?;
                last_frame = Instant::now();
                if let Some(start) = first_input.take() {
                    if let Some(trace) = trace.as_mut() {
                        writeln!(
                            trace,
                            "{}",
                            json!({"input_to_frame_ms": start.elapsed().as_secs_f64()*1000.0, "frame_monotonic_ns": monotonic_ns(), "frame_unix_ns": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos() as u64, "events": app.event_counts().iter().sum::<usize>(), "files": app.event_counts(), "visible_files": app.visible_files(), "inputs":input_count})
                        )?;
                        trace.flush()?;
                    }
                    input_count = 0;
                }
            }
            let wait = if app.dirty() {
                Duration::from_millis(2)
            } else {
                Duration::from_millis(8)
            };
            // poll wakes immediately for a key while bounding worker-notification latency.
            let _ = event::poll(wait)?;
        }
        Ok(())
    })();
    drop(app);
    let _ = execute!(io::stdout(), DisableMouseCapture);
    ratatui::restore();
    result?;
    Ok(())
}

// A shared monotonic clock makes PTY measurements immune to wall-clock corrections.
#[cfg(unix)]
fn monotonic_ns() -> Option<u64> {
    let at = rustix::time::clock_gettime(rustix::time::ClockId::Monotonic);
    Some(at.tv_sec as u64 * 1_000_000_000 + at.tv_nsec as u64)
}
#[cfg(not(unix))]
fn monotonic_ns() -> Option<u64> {
    None
}

fn color(color: ratatui::style::Color) -> String {
    use ratatui::style::Color;
    match color {
        Color::Rgb(r, g, b) => format!("#{r:02x}{g:02x}{b:02x}"),
        Color::Black => "#000000".into(),
        Color::White => "#eeeeee".into(),
        Color::Gray => "#aaaaaa".into(),
        Color::DarkGray => "#667080".into(),
        Color::Cyan => "#64c6cb".into(),
        Color::Yellow => "#e5c07b".into(),
        Color::LightBlue => "#7aa9e5".into(),
        Color::Magenta => "#bf91d9".into(),
        _ => "#d9dee8".into(),
    }
}
fn help() {
    println!(
        "agent-tui FILE [FILE ...] [--watch DIR ...]\nagent-tui --watch DIR\n\nRead-only observer. One FILE opens directly; multiple files or --watch enable the workspace.\nWorkspace Ctrl-W: h/j/k/l focus, n/p tabs, v/s split, H/J/K/L move, c close, z maximize, o open, r resize. Esc cancels.\n Missing sources are awaited; --watch scans recursively each second without directory symlinks. Each session/end freezes only that file. Quitting never stops agents.\n? help · / search · : commands · s stats · End follow · q quit\n\nOffline: --fold FILE | --prefixes FILE | --inspect FILE\nSnapshot: --snapshot FILE cells.json (120×42 Ratatui TestBackend)\nWorkspace snapshot: --workspace-snapshot cells.json FILE... (160×54, up to four panes)\nMeasurement: agent-tui FILE --trace-input trace.jsonl"
    );
}
