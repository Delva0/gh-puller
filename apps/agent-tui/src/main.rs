//! The CLI supports observation and deterministic offline replay without a model call.
use agent_tui::{
    hyperlinks,
    model::{Event as AgentEvent, Model},
    reader,
    ui::App,
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
    sync::atomic::Ordering,
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
    let file = PathBuf::from(first);
    let trace_path = args.next();
    let mut trace = if trace_path.as_deref() == Some("--trace-input") {
        Some(File::create(args.next().ok_or("missing trace path")?)?)
    } else {
        None
    };
    let (updates, stop) = reader::spawn(&file);
    let mut app = App::new(file.display().to_string());
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
        while !app.quit {
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
            for _ in 0..2 {
                let Ok(change) = updates.try_recv() else {
                    break;
                };
                app.apply(change);
            }
            app.poll_layout();
            if app.dirty && last_frame.elapsed() >= Duration::from_micros(16667) {
                let frame = terminal.draw(|f| app.render(f))?;
                links.write(frame.buffer, &app.hyperlinks(), &mut io::stdout())?;
                last_frame = Instant::now();
                if let Some(start) = first_input.take() {
                    if let Some(trace) = trace.as_mut() {
                        writeln!(
                            trace,
                            "{}",
                            json!({"input_to_frame_ms": start.elapsed().as_secs_f64()*1000.0, "frame_unix_ns": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos() as u64, "events": app.summary.events, "inputs":input_count})
                        )?;
                        trace.flush()?;
                    }
                    input_count = 0;
                }
            }
            let wait = if app.dirty {
                Duration::from_millis(2)
            } else {
                Duration::from_millis(8)
            };
            // poll wakes immediately for a key while bounding worker-notification latency.
            let _ = event::poll(wait)?;
        }
        Ok(())
    })();
    stop.store(true, Ordering::Relaxed);
    let _ = execute!(io::stdout(), DisableMouseCapture);
    ratatui::restore();
    result?;
    Ok(())
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
        "agent-tui <events.jsonl>\n\nRead-only observer. Missing files are awaited; session/end freezes observation.\n? help · / search · : commands · s stats · End follow · q quit\n\nOffline: --fold FILE | --prefixes FILE | --inspect FILE\nSnapshot: --snapshot FILE cells.json (120×42 Ratatui TestBackend)\nMeasurement: agent-tui FILE --trace-input trace.jsonl"
    );
}
