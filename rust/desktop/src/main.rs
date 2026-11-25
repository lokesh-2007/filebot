#![windows_subsystem = "windows"]
//! Small Win32-only desktop shell. It intentionally avoids a web runtime and keeps
//! Ollama/embedding work behind explicit user actions.

use filebot_core::{
    agent::TurnMetrics,
    agent::ollama_runtime::{BotRuntime, FileBotTools, LocalOllamaClient, build_runtime},
    config::{expand_path, load_settings},
    daemon::{LifecycleManager, LifecycleState, WatchService},
    executor::CommandRunner,
    retrieval::index_worker::{IndexQueueService, IndexWorker},
    storage::{Database, SearchHit},
};
use std::{
    ffi::c_void,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
        mpsc::SyncSender,
    },
    thread,
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::{HWND, LPARAM, LRESULT, WPARAM},
    Graphics::Gdi::{
        BeginPaint, CreateSolidBrush, DT_CENTER, DT_SINGLELINE, DT_VCENTER, DeleteObject,
        DrawTextW, EndPaint, FillRect, InvalidateRect, SetBkMode, SetTextColor, TRANSPARENT,
    },
    System::LibraryLoader::GetModuleHandleW,
    UI::{
        Input::KeyboardAndMouse::{
            MOD_ALT, MOD_CONTROL, MOD_SHIFT, RegisterHotKey, UnregisterHotKey, VK_SPACE,
        },
        WindowsAndMessaging::{
            BS_PUSHBUTTON, CreateWindowExW, DefWindowProcW, DispatchMessageW, ES_AUTOHSCROLL,
            ES_MULTILINE, ES_READONLY, GWLP_USERDATA, GetMessageW, GetSystemMetrics,
            GetWindowLongPtrW, GetWindowRect, GetWindowTextW, HTCAPTION, HWND_TOPMOST, IDC_ARROW,
            IsWindowVisible, LoadCursorW, MSG, PostQuitMessage, RegisterClassW, SW_HIDE, SW_SHOW,
            SWP_NOACTIVATE, SWP_NOSIZE, SetTimer, SetWindowLongPtrW, SetWindowPos, SetWindowTextW,
            ShowWindow, TranslateMessage, WM_CLOSE, WM_COMMAND, WM_CREATE, WM_DESTROY,
            WM_EXITSIZEMOVE, WM_HOTKEY, WM_LBUTTONUP, WM_NCHITTEST, WM_TIMER, WNDCLASSW, WS_BORDER,
            WS_CHILD, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_HSCROLL, WS_OVERLAPPED,
            WS_TABSTOP, WS_VISIBLE, WS_VSCROLL,
        },
    },
};

const CLASS: &str = "FileBotNativeShell";
const AVATAR_CLASS: &str = "FileBotAvatar";
const HOTKEY_ID: i32 = 1;
const TIMER_ID: usize = 7;
const ID_PROMPT: usize = 101;
const ID_SEARCH: usize = 102;
const ID_ASK: usize = 103;
const ID_CANCEL: usize = 104;
const ID_OUTPUT: usize = 105;
const ID_APPROVE: usize = 106;
const ID_REJECT: usize = 107;
const ID_PENDING: usize = 108;
const ID_EXIT: usize = 109;

type Runtime = BotRuntime<LocalOllamaClient, FileBotTools>;
type Lifecycle = LifecycleManager<Runtime>;

enum UiEvent {
    Delta(String),
    SearchFinished(Result<Vec<SearchHit>, String>),
    Finished(Result<TurnMetrics, String>),
}

struct AppState {
    db: Database,
    lifecycle: Arc<Lifecycle>,
    runner: Arc<CommandRunner>,
    tx: SyncSender<UiEvent>,
    rx: mpsc::Receiver<UiEvent>,
    avatar: HWND,
    popover: HWND,
    prompt: HWND,
    output: HWND,
    pending_label: HWND,
    busy: bool,
    searching: bool,
    cancel: Arc<AtomicBool>,
    pending_id: Option<String>,
    last_pending_refresh: Instant,
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

#[allow(clippy::too_many_arguments)] // Values map directly to Win32 child-control geometry.
fn create_control(
    parent: HWND,
    class: &str,
    label: &str,
    id: usize,
    style: u32,
    x: i32,
    y: i32,
    w: i32,
    h: i32,
) -> HWND {
    let class = wide(class);
    let label = wide(label);
    unsafe {
        CreateWindowExW(
            0,
            class.as_ptr(),
            label.as_ptr(),
            WS_CHILD | WS_VISIBLE | WS_TABSTOP | style,
            x,
            y,
            w,
            h,
            parent,
            id as _,
            GetModuleHandleW(std::ptr::null()),
            std::ptr::null(),
        )
    }
}

unsafe extern "system" fn window_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    if msg == WM_CREATE {
        let create = unsafe {
            &*(lparam as *const windows_sys::Win32::UI::WindowsAndMessaging::CREATESTRUCTW)
        };
        unsafe {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, create.lpCreateParams as isize);
        }
        let state = unsafe { &mut *(create.lpCreateParams as *mut AppState) };
        state.popover = hwnd;
        state.prompt = create_control(
            hwnd,
            "EDIT",
            "",
            ID_PROMPT,
            WS_BORDER | ES_AUTOHSCROLL as u32,
            14,
            14,
            440,
            30,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Search",
            ID_SEARCH,
            BS_PUSHBUTTON as u32,
            464,
            14,
            85,
            30,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Ask",
            ID_ASK,
            BS_PUSHBUTTON as u32,
            555,
            14,
            85,
            30,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Cancel",
            ID_CANCEL,
            BS_PUSHBUTTON as u32,
            646,
            14,
            85,
            30,
        );
        state.output = create_control(
            hwnd,
            "EDIT",
            "Sleeping. Search uses the local index; Ask wakes the model.",
            ID_OUTPUT,
            WS_BORDER | ES_MULTILINE as u32 | ES_READONLY as u32 | WS_VSCROLL | WS_HSCROLL,
            14,
            54,
            717,
            300,
        );
        state.pending_label = create_control(
            hwnd,
            "STATIC",
            "No command awaiting approval.",
            ID_PENDING,
            0,
            14,
            362,
            490,
            58,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Approve",
            ID_APPROVE,
            BS_PUSHBUTTON as u32,
            520,
            362,
            100,
            30,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Reject",
            ID_REJECT,
            BS_PUSHBUTTON as u32,
            630,
            362,
            100,
            30,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Exit FileBot",
            ID_EXIT,
            BS_PUSHBUTTON as u32,
            14,
            426,
            110,
            28,
        );
        unsafe {
            SetTimer(hwnd, TIMER_ID, 80, None);
        }
        return 0;
    }
    let pointer = unsafe { GetWindowLongPtrW(hwnd, GWLP_USERDATA) } as *mut AppState;
    if pointer.is_null() {
        return unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) };
    }
    let state = unsafe { &mut *pointer };
    match msg {
        WM_HOTKEY => {
            toggle(state);
            0
        }
        WM_COMMAND => {
            let id = wparam & 0xffff;
            match id {
                ID_SEARCH => search(state),
                ID_ASK => ask(state),
                ID_CANCEL => {
                    state.cancel.store(true, Ordering::Release);
                    set_output(
                        state,
                        "Cancellation requested. The active response will stop at the next stream boundary.",
                    );
                }
                ID_APPROVE => approve(state, true),
                ID_REJECT => approve(state, false),
                ID_EXIT => unsafe {
                    windows_sys::Win32::UI::WindowsAndMessaging::DestroyWindow(state.avatar);
                    windows_sys::Win32::UI::WindowsAndMessaging::DestroyWindow(hwnd);
                },
                _ => {}
            }
            0
        }
        WM_TIMER => {
            while let Ok(event) = state.rx.try_recv() {
                match event {
                    UiEvent::Delta(delta) => append_output(state.output, &delta),
                    UiEvent::SearchFinished(result) => {
                        state.searching = false;
                        match result {
                            Ok(hits) if hits.is_empty() => set_output(
                                state,
                                "No indexed matches yet. Confirm the configured folders and wait for indexing.",
                            ),
                            Ok(hits) => {
                                let body = hits
                                    .iter()
                                    .map(|hit| {
                                        format!(
                                            "{}\r\n{}\r\n{}\r\n",
                                            hit.file_name, hit.path, hit.snippet
                                        )
                                    })
                                    .collect::<Vec<_>>()
                                    .join("\r\n");
                                set_output(state, &body);
                            }
                            Err(error) => set_output(state, &format!("Search failed: {error}")),
                        }
                    }
                    UiEvent::Finished(result) => {
                        state.busy = false;
                        match result {
                            Ok(metrics) => append_output(
                                state.output,
                                &format!(
                                    "\r\n\r\nTiming · first token {} ms · Ollama preflight {} ms · total {} ms · {} response chars",
                                    metrics.time_to_first_token.map_or_else(
                                        || "n/a".to_owned(),
                                        |t| t.as_millis().to_string()
                                    ),
                                    metrics.model_preflight.as_millis(),
                                    metrics.total.as_millis(),
                                    metrics.response_chars,
                                ),
                            ),
                            Err(error) => {
                                append_output(state.output, &format!("\r\n\r\n{error}"));
                            }
                        }
                    }
                }
            }
            if state.last_pending_refresh.elapsed() >= Duration::from_millis(500) {
                refresh_pending(state);
                state.last_pending_refresh = Instant::now();
                unsafe {
                    InvalidateRect(state.avatar, std::ptr::null(), 0);
                }
            }
            0
        }
        WM_CLOSE => {
            unsafe {
                ShowWindow(hwnd, SW_HIDE);
            }
            0
        }
        WM_DESTROY => {
            unsafe {
                PostQuitMessage(0);
            }
            0
        }
        _ => unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) },
    }
}

fn read_prompt(hwnd: HWND) -> String {
    let mut buffer = vec![0u16; 4097];
    let len =
        unsafe { GetWindowTextW(hwnd, buffer.as_mut_ptr(), buffer.len() as i32) }.max(0) as usize;
    String::from_utf16_lossy(&buffer[..len])
}

fn set_output(state: &AppState, text: &str) {
    let wide = wide(text);
    unsafe {
        SetWindowTextW(state.output, wide.as_ptr());
    }
}

fn append_output(hwnd: HWND, text: &str) {
    // Keep the visible transcript bounded so long sessions cannot grow the UI buffer.
    let mut current = vec![0u16; 32_768];
    let len =
        unsafe { GetWindowTextW(hwnd, current.as_mut_ptr(), current.len() as i32) }.max(0) as usize;
    let mut content = String::from_utf16_lossy(&current[..len]);
    content.push_str(text);
    let mut utf16: Vec<u16> = content.encode_utf16().collect();
    if utf16.len() > 30_000 {
        utf16.drain(..utf16.len() - 30_000);
    }
    let content = String::from_utf16_lossy(&utf16);
    let wide = wide(&content);
    unsafe {
        SetWindowTextW(hwnd, wide.as_ptr());
    }
}

fn search(state: &mut AppState) {
    if state.searching {
        return;
    }
    let query = read_prompt(state.prompt);
    if query.trim().is_empty() {
        set_output(state, "Enter a filename or search phrase.");
        return;
    }
    state.searching = true;
    set_output(state, "Searching the local SQLite index…");
    let db = state.db.clone();
    let tx = state.tx.clone();
    thread::spawn(move || {
        let result = db
            .search_lexical(&query, 12)
            .map_err(|error| error.to_string());
        let _ = tx.send(UiEvent::SearchFinished(result));
    });
}

fn ask(state: &mut AppState) {
    if state.busy {
        set_output(state, "A response is already running.");
        return;
    }
    let prompt = read_prompt(state.prompt);
    if prompt.trim().is_empty() {
        set_output(state, "Enter a question first.");
        return;
    }
    state.busy = true;
    state.cancel.store(false, Ordering::Release);
    state.lifecycle.wake();
    set_output(state, "Waking local model…\r\n");
    let lifecycle = Arc::clone(&state.lifecycle);
    let tx = state.tx.clone();
    let cancel = Arc::clone(&state.cancel);
    thread::spawn(move || {
        let result = lifecycle
            .ask_streaming_profiled(&prompt, |part| {
                !cancel.load(Ordering::Acquire) && tx.send(UiEvent::Delta(part.to_owned())).is_ok()
            })
            .map(|(_answer, metrics)| metrics)
            .map_err(|error| error.to_string());
        let _ = tx.send(UiEvent::Finished(result));
    });
}

fn refresh_pending(state: &mut AppState) {
    if let Ok(rows) = state.db.pending_commands() {
        state.pending_id = rows.first().map(|row| row.proposal_id.clone());
        if let Some(row) = rows.first() {
            let msg = format!("CMD awaiting approval: {}\r\n{}", row.command, row.reason);
            let wide = wide(&msg);
            unsafe {
                SetWindowTextW(state.pending_label, wide.as_ptr());
            }
        } else {
            let wide = wide("No command awaiting approval.");
            unsafe {
                SetWindowTextW(state.pending_label, wide.as_ptr());
            }
        }
    }
}

fn approve(state: &mut AppState, accepted: bool) {
    let Some(id) = state.pending_id.clone() else {
        append_output(state.output, "\r\nNo pending command proposal.");
        return;
    };
    match state.db.decide_command(&id, accepted) {
        Ok(true) if accepted => {
            let runner = Arc::clone(&state.runner);
            let tx = state.tx.clone();
            thread::spawn(move || {
                let result = runner
                    .execute(&id)
                    .map(|result| {
                        format!(
                            "CMD finished (exit {}).\r\n{}",
                            result.exit_code, result.stdout
                        )
                    })
                    .unwrap_or_else(|error| format!("CMD failed: {error}"));
                let _ = tx.send(UiEvent::Delta(format!("\r\n\r\n{result}")));
            });
            append_output(state.output, "\r\nApproved. Running the staged command…");
        }
        Ok(true) => append_output(state.output, "\r\nCommand proposal rejected."),
        Ok(false) => append_output(state.output, "\r\nProposal is no longer pending."),
        Err(error) => append_output(
            state.output,
            &format!("\r\nCould not update approval: {error}"),
        ),
    }
    state.pending_id = None;
}

fn toggle(state: &AppState) {
    let visible = unsafe { IsWindowVisible(state.popover) } != 0;
    if visible {
        unsafe {
            ShowWindow(state.popover, SW_HIDE);
        }
    } else {
        unsafe {
            ShowWindow(state.popover, SW_SHOW);
            SetWindowPos(
                state.popover,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOSIZE | SWP_NOACTIVATE,
            );
        }
    }
}

unsafe extern "system" fn avatar_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    if msg == WM_CREATE {
        let create = unsafe {
            &*(lparam as *const windows_sys::Win32::UI::WindowsAndMessaging::CREATESTRUCTW)
        };
        unsafe {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, create.lpCreateParams as isize);
        }
        return 0;
    }
    if msg == WM_NCHITTEST {
        return HTCAPTION as LRESULT;
    }
    if msg == WM_EXITSIZEMOVE {
        let mut rect = windows_sys::Win32::Foundation::RECT::default();
        if unsafe { GetWindowRect(hwnd, &mut rect) } != 0 {
            let screen_w = unsafe {
                GetSystemMetrics(windows_sys::Win32::UI::WindowsAndMessaging::SM_CXSCREEN)
            };
            let screen_h = unsafe {
                GetSystemMetrics(windows_sys::Win32::UI::WindowsAndMessaging::SM_CYSCREEN)
            };
            let x = if rect.left + 29 < screen_w / 2 {
                16
            } else {
                screen_w - 74
            };
            let y = if rect.top + 29 < screen_h / 2 {
                60
            } else {
                screen_h - 104
            };
            unsafe {
                SetWindowPos(hwnd, HWND_TOPMOST, x, y, 58, 58, SWP_NOACTIVATE);
            }
        }
        return 0;
    }
    if msg == WM_LBUTTONUP {
        let pointer = unsafe { GetWindowLongPtrW(hwnd, GWLP_USERDATA) } as *mut AppState;
        if !pointer.is_null() {
            toggle(unsafe { &*pointer });
        }
        return 0;
    }
    if msg == windows_sys::Win32::UI::WindowsAndMessaging::WM_PAINT {
        let mut paint = windows_sys::Win32::Graphics::Gdi::PAINTSTRUCT::default();
        let dc = unsafe { BeginPaint(hwnd, &mut paint) };
        let mut rect = windows_sys::Win32::Foundation::RECT {
            left: 0,
            top: 0,
            right: 58,
            bottom: 58,
        };
        let pointer = unsafe { GetWindowLongPtrW(hwnd, GWLP_USERDATA) } as *mut AppState;
        let color = if pointer.is_null() {
            0x00904420
        } else {
            let state = unsafe { &*pointer };
            if state.pending_id.is_some() {
                0x002020d0
            } else if state.lifecycle.state() == LifecycleState::Active {
                0x00307820
            } else {
                0x00904420
            }
        };
        let brush = unsafe { CreateSolidBrush(color) };
        unsafe {
            FillRect(dc, &rect, brush);
            DeleteObject(brush);
            SetBkMode(dc, TRANSPARENT as i32);
            SetTextColor(dc, 0x00ffffff);
            let caption = wide("FB");
            DrawTextW(
                dc,
                caption.as_ptr(),
                2,
                &mut rect,
                DT_CENTER | DT_VCENTER | DT_SINGLELINE,
            );
            EndPaint(hwnd, &paint);
        }
        return 0;
    }
    unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) }
}

fn register_class(
    class_name: &str,
    procedure: unsafe extern "system" fn(HWND, u32, WPARAM, LPARAM) -> LRESULT,
    instance: windows_sys::Win32::Foundation::HINSTANCE,
) -> Result<(), String> {
    let name = wide(class_name);
    let cursor = unsafe { LoadCursorW(std::ptr::null_mut(), IDC_ARROW) };
    let class = WNDCLASSW {
        style: 0,
        lpfnWndProc: Some(procedure),
        cbClsExtra: 0,
        cbWndExtra: 0,
        hInstance: instance,
        hIcon: std::ptr::null_mut(),
        hCursor: cursor,
        hbrBackground: (windows_sys::Win32::Graphics::Gdi::COLOR_WINDOW + 1) as _,
        lpszMenuName: std::ptr::null(),
        lpszClassName: name.as_ptr(),
    };
    if unsafe { RegisterClassW(&class) } == 0 {
        Err(format!("Could not register window class {class_name}"))
    } else {
        Ok(())
    }
}

fn app_paths() -> Result<(PathBuf, PathBuf), String> {
    let mut roots = Vec::new();
    if let Some(exe_dir) = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(PathBuf::from))
    {
        roots.push(exe_dir.clone());
        roots.push(exe_dir.join(".."));
        roots.push(exe_dir.join("../.."));
    }
    roots.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."));
    let root = roots
        .into_iter()
        .find(|root| root.join("config/settings.yaml").is_file())
        .ok_or_else(|| "Could not find config/settings.yaml next to FileBot".to_owned())?;
    Ok((
        root.join("config/settings.yaml"),
        root.join("config/cmd_policy.yaml"),
    ))
}

fn run() -> Result<(), String> {
    let (settings_path, policy_path) = app_paths()?;
    let settings = load_settings(&settings_path).map_err(|e| e.to_string())?;
    let db_path = expand_path(&settings.app.database_path).map_err(|e| e.to_string())?;
    let db = Database::new(db_path)
        .with_busy_timeout(2_000)
        .map_err(|e| e.to_string())?;
    db.initialize().map_err(|e| e.to_string())?;
    let _watcher = WatchService::start(&settings, db.clone()).map_err(|e| e.to_string())?;
    let indexer = Arc::new(IndexWorker::new(&settings, db.clone()).map_err(|e| e.to_string())?);
    let _index_queue = IndexQueueService::start(
        indexer,
        settings.index.background_batch_size,
        Duration::from_secs_f64(settings.index.background_poll_seconds),
    )
    .map_err(|e| e.to_string())?;
    let runtime = build_runtime(&settings, db.clone(), &policy_path).map_err(|e| e.to_string())?;
    let lifecycle = Arc::new(
        LifecycleManager::new(
            runtime,
            Duration::from_secs(settings.runtime.idle_unload_seconds),
        )
        .map_err(|e| e.to_string())?,
    );
    let runner = Arc::new(
        CommandRunner::new(db.clone(), policy_path, Duration::from_secs(60))
            .map_err(|e| e.to_string())?,
    );
    let (tx, rx) = mpsc::sync_channel(128);
    let mut app = Box::new(AppState {
        db,
        lifecycle,
        runner,
        tx,
        rx,
        avatar: std::ptr::null_mut(),
        popover: std::ptr::null_mut(),
        prompt: std::ptr::null_mut(),
        output: std::ptr::null_mut(),
        pending_label: std::ptr::null_mut(),
        busy: false,
        searching: false,
        cancel: Arc::new(AtomicBool::new(false)),
        pending_id: None,
        last_pending_refresh: Instant::now(),
    });
    let instance = unsafe { GetModuleHandleW(std::ptr::null()) };
    register_class(CLASS, window_proc, instance)?;
    let class_name = wide(CLASS);
    let popover_name = wide("FileBot · Local search and assistant");
    let screen_w =
        unsafe { GetSystemMetrics(windows_sys::Win32::UI::WindowsAndMessaging::SM_CXSCREEN) };
    let screen_h =
        unsafe { GetSystemMetrics(windows_sys::Win32::UI::WindowsAndMessaging::SM_CYSCREEN) };
    let popover = unsafe {
        CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            class_name.as_ptr(),
            popover_name.as_ptr(),
            WS_OVERLAPPED,
            (screen_w - 780).max(0),
            (screen_h - 590).max(0),
            760,
            470,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            instance,
            (&mut *app as *mut AppState).cast::<c_void>(),
        )
    };
    if popover.is_null() {
        return Err("Could not create FileBot window".into());
    }
    app.popover = popover;
    // The compact topmost avatar toggles the popover and shares the same keyboard shortcut.
    register_class(AVATAR_CLASS, avatar_proc, instance)?;
    let avatar_class = wide(AVATAR_CLASS);
    let avatar_title = wide("FB");
    let screen_w =
        unsafe { GetSystemMetrics(windows_sys::Win32::UI::WindowsAndMessaging::SM_CXSCREEN) };
    let screen_h =
        unsafe { GetSystemMetrics(windows_sys::Win32::UI::WindowsAndMessaging::SM_CYSCREEN) };
    let avatar = unsafe {
        CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            avatar_class.as_ptr(),
            avatar_title.as_ptr(),
            WS_VISIBLE | BS_PUSHBUTTON as u32,
            screen_w - 74,
            screen_h - 104,
            58,
            58,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            instance,
            (&mut *app as *mut AppState).cast::<c_void>(),
        )
    };
    if avatar.is_null() {
        return Err("Could not create FileBot avatar".into());
    }
    app.avatar = avatar;
    let (modifiers, key) = parse_hotkey(&settings.runtime.hotkey)?;
    if unsafe { RegisterHotKey(popover, HOTKEY_ID, modifiers, key) } == 0 {
        return Err(format!(
            "Could not register configured hotkey {}",
            settings.runtime.hotkey
        ));
    }
    let mut message = MSG::default();
    loop {
        let result = unsafe { GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) };
        if result <= 0 {
            break;
        }
        unsafe {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
    unsafe {
        UnregisterHotKey(popover, HOTKEY_ID);
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        let message = wide(&format!("FileBot could not start: {error}"));
        unsafe {
            windows_sys::Win32::UI::WindowsAndMessaging::MessageBoxW(
                std::ptr::null_mut(),
                message.as_ptr(),
                wide("FileBot").as_ptr(),
                0x10,
            );
        }
    }
}

fn parse_hotkey(value: &str) -> Result<(u32, u32), String> {
    let normalized = value.to_ascii_lowercase().replace(['<', '>'], "");
    let tokens: Vec<&str> = normalized.split('+').map(str::trim).collect();
    let mut modifiers = 0;
    for token in &tokens {
        match *token {
            "ctrl" | "control" => modifiers |= MOD_CONTROL,
            "alt" => modifiers |= MOD_ALT,
            "shift" => modifiers |= MOD_SHIFT,
            _ => {}
        }
    }
    let key_name = tokens.last().copied().unwrap_or_default();
    let key = if key_name == "space" {
        VK_SPACE as u32
    } else if let Some(number) = key_name
        .strip_prefix('f')
        .and_then(|part| part.parse::<u32>().ok())
        .filter(|number| (1..=12).contains(number))
    {
        0x70 + number - 1
    } else {
        return Err(format!(
            "Unsupported hotkey key {key_name:?}; use Space or F1 through F12"
        ));
    };
    if modifiers == 0 {
        return Err("Configured hotkey must include Ctrl, Alt, or Shift".into());
    }
    Ok((modifiers, key))
}
