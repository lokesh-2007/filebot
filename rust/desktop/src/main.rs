#![windows_subsystem = "windows"]
//! Small Win32-only desktop shell. It intentionally avoids a web runtime and keeps
//! Ollama/embedding work behind explicit user actions.

use filebot_core::{
    agent::TurnMetrics,
    agent::ollama_runtime::{
        BotRuntime, FileBotTools, LocalOllamaClient, build_runtime, open_indexed_path,
    },
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
    System::{
        ProcessStatus::{K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS},
        Threading::GetCurrentProcess,
    },
    UI::{
        Input::KeyboardAndMouse::{
            MOD_ALT, MOD_CONTROL, MOD_SHIFT, RegisterHotKey, UnregisterHotKey, VK_SPACE,
        },
        WindowsAndMessaging::{
            BS_PUSHBUTTON, CreateWindowExW, DefWindowProcW, DispatchMessageW, ES_AUTOHSCROLL,
            ES_AUTOVSCROLL, ES_MULTILINE, ES_READONLY, GWLP_USERDATA, GetMessageW, GetSystemMetrics,
            GetWindowLongPtrW, GetWindowRect, GetWindowTextLengthW, GetWindowTextW, HTCAPTION,
            HWND_TOPMOST, IDC_ARROW,
            IsWindowVisible, LB_ADDSTRING, LB_ERR, LB_GETCURSEL, LB_RESETCONTENT, LBN_DBLCLK,
            LBS_NOTIFY, LoadCursorW, MSG, PostQuitMessage, RegisterClassW, SW_HIDE, SW_SHOW,
            SWP_NOACTIVATE, SWP_NOSIZE, SendMessageW, SetTimer, SetWindowLongPtrW, SetWindowPos,
            SetWindowTextW, ShowWindow, TranslateMessage, WM_CLOSE, WM_COMMAND, WM_CREATE,
            WM_DESTROY, WM_EXITSIZEMOVE, WM_HOTKEY, WM_NCDESTROY, WM_NCHITTEST, WM_NCLBUTTONUP,
            WM_TIMER,
            WNDCLASSW, WS_BORDER, WS_CHILD, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST,
            WS_HSCROLL, WS_OVERLAPPED, WS_OVERLAPPEDWINDOW, WS_TABSTOP, WS_VISIBLE, WS_VSCROLL,
        },
    },
};

const CLASS: &str = "FileBotNativeShell";
const AVATAR_CLASS: &str = "FileBotAvatar";
const GLOSSARY_CLASS: &str = "FileBotGlossaryEditor";
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
const ID_STATUS: usize = 110;
const ID_RESULTS: usize = 111;
const ID_OPEN_SELECTED: usize = 112;
const ID_HELPFUL: usize = 113;
const ID_UNHELPFUL: usize = 114;
const ID_GLOSSARY: usize = 115;
const ID_GLOSSARY_TEXT: usize = 116;
const ID_GLOSSARY_SAVE: usize = 117;
const ID_GLOSSARY_CANCEL: usize = 118;

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
    status_label: HWND,
    results_list: HWND,
    pending_label: HWND,
    busy: bool,
    searching: bool,
    cancel: Arc<AtomicBool>,
    pending_id: Option<String>,
    search_results: Vec<SearchHit>,
    file_roots: Vec<PathBuf>,
    last_pending_refresh: Instant,
    last_status_refresh: Instant,
    glossary_window: HWND,
}

struct GlossaryWindowState {
    db: Database,
    owner: HWND,
    editor: HWND,
    status: HWND,
    initial_text: String,
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
            274,
            717,
            216,
        );
        state.status_label =
            create_control(hwnd, "STATIC", "Starting…", ID_STATUS, 0, 14, 54, 717, 20);
        state.results_list = create_control(
            hwnd,
            "LISTBOX",
            "",
            ID_RESULTS,
            WS_BORDER | WS_VSCROLL | LBS_NOTIFY as u32,
            14,
            80,
            717,
            150,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Open selected file",
            ID_OPEN_SELECTED,
            BS_PUSHBUTTON as u32,
            14,
            238,
            160,
            28,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Helpful",
            ID_HELPFUL,
            BS_PUSHBUTTON as u32,
            184,
            238,
            90,
            28,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Not helpful",
            ID_UNHELPFUL,
            BS_PUSHBUTTON as u32,
            284,
            238,
            100,
            28,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Edit vocabulary",
            ID_GLOSSARY,
            BS_PUSHBUTTON as u32,
            394,
            238,
            130,
            28,
        );
        state.pending_label = create_control(
            hwnd,
            "STATIC",
            "No command awaiting approval.",
            ID_PENDING,
            0,
            14,
            500,
            490,
            68,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Approve",
            ID_APPROVE,
            BS_PUSHBUTTON as u32,
            520,
            500,
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
            500,
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
            590,
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
            let notification = (wparam >> 16) & 0xffff;
            match id {
                ID_SEARCH => search(state),
                ID_OPEN_SELECTED => open_selected(state),
                ID_HELPFUL => record_feedback(state, true),
                ID_UNHELPFUL => record_feedback(state, false),
                ID_GLOSSARY => open_glossary_editor(state),
                ID_RESULTS if notification == LBN_DBLCLK as usize => open_selected(state),
                ID_ASK => ask(state),
                ID_CANCEL => {
                    state.cancel.store(true, Ordering::Release);
                    set_output(
                        state,
                        "Cancellation requested. FileBot will stop model startup or the active response.",
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
                            Ok(hits) if hits.is_empty() => {
                                state.search_results.clear();
                                unsafe {
                                    SendMessageW(state.results_list, LB_RESETCONTENT, 0, 0);
                                }
                                set_output(
                                    state,
                                    "No indexed matches yet. Confirm the configured folders and wait for indexing.",
                                );
                            }
                            Ok(hits) => {
                                state.search_results = hits;
                                unsafe {
                                    SendMessageW(state.results_list, LB_RESETCONTENT, 0, 0);
                                }
                                for hit in &state.search_results {
                                    let label =
                                        wide(&format!("{}  ·  {}", hit.file_name, hit.path));
                                    unsafe {
                                        SendMessageW(
                                            state.results_list,
                                            LB_ADDSTRING,
                                            0,
                                            label.as_ptr() as LPARAM,
                                        );
                                    }
                                }
                                set_output(
                                    state,
                                    &format!(
                                        "Found {} files. Double-click a result or select it and choose Open selected file.",
                                        state.search_results.len()
                                    ),
                                );
                            }
                            Err(error) => {
                                state.search_results.clear();
                                set_output(state, &format!("Search failed: {error}"));
                            }
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
            if state.last_status_refresh.elapsed() >= Duration::from_secs(1) {
                refresh_status(state);
                state.last_status_refresh = Instant::now();
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
    const MAX_TRANSCRIPT_UNITS: usize = 30_000;
    const TRIMMED_TRANSCRIPT_UNITS: usize = 24_000;
    // Avoid rebuilding the entire transcript for every streamed token. Keep the
    // edit control read-only for users, temporarily enabling programmatic append.
    let addition = wide(text);
    unsafe {
        SendMessageW(hwnd, 0x00cf, 0, 0); // EM_SETREADONLY(FALSE)
        let length = GetWindowTextLengthW(hwnd).max(0) as usize;
        SendMessageW(hwnd, 0x00b1, length, length as LPARAM); // EM_SETSEL(end, end)
        SendMessageW(hwnd, 0x00c2, 0, addition.as_ptr() as LPARAM); // EM_REPLACESEL
        SendMessageW(hwnd, 0x00cf, 1, 0); // EM_SETREADONLY(TRUE)
        let length = GetWindowTextLengthW(hwnd).max(0) as usize;
        if length > MAX_TRANSCRIPT_UNITS {
            let mut current = vec![0u16; length + 1];
            let copied = GetWindowTextW(hwnd, current.as_mut_ptr(), current.len() as i32)
                .max(0) as usize;
            let content = String::from_utf16_lossy(&current[..copied]);
            let retained = transcript_suffix(&content, TRIMMED_TRANSCRIPT_UNITS);
            let retained_wide = wide(retained);
            SetWindowTextW(hwnd, retained_wide.as_ptr());
            let end = retained.encode_utf16().count();
            SendMessageW(hwnd, 0x00b1, end, end as LPARAM);
        }
        SendMessageW(hwnd, 0x00b7, 0, 0); // EM_SCROLLCARET
    }
}

fn transcript_suffix(text: &str, max_utf16_units: usize) -> &str {
    let mut units = 0;
    let mut start = text.len();
    for (index, character) in text.char_indices().rev() {
        let width = character.len_utf16();
        if units + width > max_utf16_units {
            break;
        }
        units += width;
        start = index;
    }
    &text[start..]
}

fn open_glossary_editor(state: &mut AppState) {
    if !state.glossary_window.is_null() {
        unsafe { ShowWindow(state.glossary_window, SW_SHOW) };
        return;
    }
    let entries = match state.db.glossary_entries() {
        Ok(entries) => entries,
        Err(error) => {
            append_output(state.output, &format!("\r\nCould not load vocabulary: {error}"));
            return;
        }
    };
    let mut rows: Vec<_> = entries.into_iter().collect();
    rows.sort_by_key(|row| row.0.to_lowercase());
    let initial_text = rows
        .into_iter()
        .map(|(alias, expansion)| format!("{alias}={expansion}"))
        .collect::<Vec<_>>()
        .join("\r\n");
    let mut editor_state = Box::new(GlossaryWindowState {
        db: state.db.clone(),
        owner: state.popover,
        editor: std::ptr::null_mut(),
        status: std::ptr::null_mut(),
        initial_text,
    });
    let class = wide(GLOSSARY_CLASS);
    let title = wide("FileBot search vocabulary");
    let window = unsafe {
        CreateWindowExW(
            WS_EX_TOOLWINDOW,
            class.as_ptr(),
            title.as_ptr(),
            WS_OVERLAPPEDWINDOW,
            120,
            120,
            620,
            520,
            state.popover,
            std::ptr::null_mut(),
            GetModuleHandleW(std::ptr::null()),
            (&mut *editor_state as *mut GlossaryWindowState).cast::<c_void>(),
        )
    };
    if window.is_null() {
        append_output(state.output, "\r\nCould not open the vocabulary editor.");
        drop(editor_state);
    } else {
        state.glossary_window = window;
        unsafe { ShowWindow(window, SW_SHOW) };
    }
}

unsafe extern "system" fn glossary_window_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    if msg == WM_CREATE {
        let create = unsafe {
            &*(lparam as *const windows_sys::Win32::UI::WindowsAndMessaging::CREATESTRUCTW)
        };
        let state = unsafe { &mut *(create.lpCreateParams as *mut GlossaryWindowState) };
        unsafe { SetWindowLongPtrW(hwnd, GWLP_USERDATA, create.lpCreateParams as isize) };
        let instruction = wide("One alias=expansion per line. Save replaces the current vocabulary.");
        let label = wide("Example: spec=product specification");
        let static_class = wide("STATIC");
        unsafe {
            CreateWindowExW(
                0,
                static_class.as_ptr(),
                instruction.as_ptr(),
                WS_CHILD | WS_VISIBLE,
                16,
                14,
                570,
                24,
                hwnd,
                std::ptr::null_mut(),
                GetModuleHandleW(std::ptr::null()),
                std::ptr::null(),
            );
            CreateWindowExW(
                0,
                static_class.as_ptr(),
                label.as_ptr(),
                WS_CHILD | WS_VISIBLE,
                16,
                38,
                570,
                22,
                hwnd,
                std::ptr::null_mut(),
                GetModuleHandleW(std::ptr::null()),
                std::ptr::null(),
            );
        }
        let edit_class = wide("EDIT");
        let text = wide(&state.initial_text);
        state.editor = unsafe {
            CreateWindowExW(
                WS_BORDER,
                edit_class.as_ptr(),
                text.as_ptr(),
                WS_CHILD | WS_VISIBLE | WS_TABSTOP | ES_MULTILINE as u32
                    | ES_AUTOVSCROLL as u32 | WS_VSCROLL,
                16,
                68,
                570,
                350,
                hwnd,
                ID_GLOSSARY_TEXT as _,
                GetModuleHandleW(std::ptr::null()),
                std::ptr::null(),
            )
        };
        unsafe { SendMessageW(state.editor, 0x00c5, 262_143, 0) };
        state.status = create_control(hwnd, "STATIC", "", 0, 0, 16, 426, 570, 24);
        create_control(
            hwnd,
            "BUTTON",
            "Save vocabulary",
            ID_GLOSSARY_SAVE,
            BS_PUSHBUTTON as u32,
            356,
            454,
            110,
            30,
        );
        create_control(
            hwnd,
            "BUTTON",
            "Cancel",
            ID_GLOSSARY_CANCEL,
            BS_PUSHBUTTON as u32,
            476,
            454,
            90,
            30,
        );
        return 0;
    }
    let pointer = unsafe { GetWindowLongPtrW(hwnd, GWLP_USERDATA) } as *mut GlossaryWindowState;
    if pointer.is_null() {
        return unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) };
    }
    let state = unsafe { &mut *pointer };
    match msg {
        WM_COMMAND => match wparam & 0xffff {
            ID_GLOSSARY_SAVE => {
                let mut buffer = vec![0u16; 262_144];
                let length = unsafe {
                    GetWindowTextW(state.editor, buffer.as_mut_ptr(), buffer.len() as i32)
                }
                .max(0) as usize;
                let text = String::from_utf16_lossy(&buffer[..length]);
                let mut entries = std::collections::HashMap::new();
                for (line_number, line) in text.lines().enumerate() {
                    let line = line.trim();
                    if line.is_empty() {
                        continue;
                    }
                    let Some((alias, expansion)) = line.split_once('=') else {
                        let message = wide(&format!(
                            "Line {} needs alias=expansion.", line_number + 1
                        ));
                        unsafe { SetWindowTextW(state.status, message.as_ptr()) };
                        return 0;
                    };
                    if entries
                        .insert(alias.trim().to_owned(), expansion.trim().to_owned())
                        .is_some()
                    {
                        let message = wide(&format!(
                            "Line {} repeats an alias; aliases must be unique.", line_number + 1
                        ));
                        unsafe { SetWindowTextW(state.status, message.as_ptr()) };
                        return 0;
                    }
                }
                match state.db.replace_glossary_entries(&entries) {
                    Ok(()) => {
                        let message = wide("Vocabulary saved. It applies to the next search.");
                        unsafe {
                            SetWindowTextW(state.status, message.as_ptr());
                            let owner_state =
                                GetWindowLongPtrW(state.owner, GWLP_USERDATA) as *mut AppState;
                            if !owner_state.is_null() {
                                append_output(
                                    (*owner_state).output,
                                    "\r\nVocabulary saved; it applies to the next search.",
                                );
                            }
                            windows_sys::Win32::UI::WindowsAndMessaging::DestroyWindow(hwnd);
                        }
                    }
                    Err(error) => {
                        let message = wide(&format!("Could not save vocabulary: {error}"));
                        unsafe { SetWindowTextW(state.status, message.as_ptr()) };
                    }
                }
                0
            }
            ID_GLOSSARY_CANCEL => {
                unsafe { windows_sys::Win32::UI::WindowsAndMessaging::DestroyWindow(hwnd) };
                0
            }
            _ => unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) },
        },
        WM_NCDESTROY => {
            let owner = state.owner;
            let owner_state = unsafe { GetWindowLongPtrW(owner, GWLP_USERDATA) } as *mut AppState;
            if !owner_state.is_null() {
                unsafe { (*owner_state).glossary_window = std::ptr::null_mut() };
            }
            unsafe {
                SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
                drop(Box::from_raw(pointer));
                DefWindowProcW(hwnd, msg, wparam, lparam)
            }
        }
        _ => unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) },
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
    state.search_results.clear();
    unsafe {
        SendMessageW(state.results_list, LB_RESETCONTENT, 0, 0);
    }
    set_output(state, "Searching the local SQLite index…");
    let db = state.db.clone();
    let tx = state.tx.clone();
    thread::spawn(move || {
        let result = db
            .expand_glossary(&query)
            .and_then(|expanded| {
                let mut hits = db.search_lexical(&expanded, 12)?;
                for hit in &mut hits {
                    hit.score += db.file_signal_boost(&hit.path)?;
                }
                hits.sort_by(|left, right| right.score.total_cmp(&left.score));
                Ok(hits)
            })
            .map_err(|error| error.to_string());
        let _ = tx.send(UiEvent::SearchFinished(result));
    });
}

fn open_selected(state: &mut AppState) {
    let selected = unsafe { SendMessageW(state.results_list, LB_GETCURSEL, 0, 0) };
    if selected == LB_ERR as isize || selected < 0 {
        append_output(state.output, "\r\nSelect a search result first.");
        return;
    }
    let Some(hit) = state.search_results.get(selected as usize) else {
        append_output(
            state.output,
            "\r\nThat search result is no longer available.",
        );
        return;
    };
    let path = hit.path.clone();
    let roots = state.file_roots.clone();
    let db = state.db.clone();
    let tx = state.tx.clone();
    thread::spawn(move || {
        let result = match open_indexed_path(&db, &roots, &path) {
            Ok(message) => UiEvent::Delta(format!("\r\n{message}")),
            Err(error) => UiEvent::Delta(format!("\r\nCould not open file: {error}")),
        };
        let _ = tx.send(result);
    });
}

fn record_feedback(state: &mut AppState, helpful: bool) {
    let selected = unsafe { SendMessageW(state.results_list, LB_GETCURSEL, 0, 0) };
    if selected == LB_ERR as isize || selected < 0 {
        append_output(state.output, "\r\nSelect a search result before rating it.");
        return;
    }
    let Some(hit) = state.search_results.get(selected as usize) else {
        append_output(
            state.output,
            "\r\nThat search result is no longer available.",
        );
        return;
    };
    let path = hit.path.clone();
    let db = state.db.clone();
    let tx = state.tx.clone();
    let vote = if helpful { "up" } else { "down" };
    thread::spawn(move || {
        let message = match db.record_file_feedback(&path, vote) {
            Ok(true) => "Feedback saved; it will influence later searches.".to_owned(),
            Ok(false) => "This file is no longer in FileBot's index.".to_owned(),
            Err(error) => format!("Could not save feedback: {error}"),
        };
        let _ = tx.send(UiEvent::Delta(format!("\r\n{message}")));
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
            .ask_streaming_profiled_cancellable(&prompt, |part| {
                !cancel.load(Ordering::Acquire) && tx.send(UiEvent::Delta(part.to_owned())).is_ok()
            }, || cancel.load(Ordering::Acquire))
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

fn refresh_status(state: &AppState) {
    let lifecycle = match state.lifecycle.state() {
        LifecycleState::Sleeping => "Sleeping",
        LifecycleState::Active => "Active",
    };
    let mut counters = PROCESS_MEMORY_COUNTERS {
        cb: std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32,
        ..Default::default()
    };
    let process = unsafe { GetCurrentProcess() };
    let memory_ok = unsafe { K32GetProcessMemoryInfo(process, &mut counters, counters.cb) } != 0;
    let memory = if memory_ok {
        format!(
            " · working set {} MB",
            counters.WorkingSetSize / (1024 * 1024)
        )
    } else {
        String::new()
    };
    let pending = if state.pending_id.is_some() {
        " · command pending approval"
    } else {
        ""
    };
    let text = wide(&format!("{lifecycle}{memory}{pending}"));
    unsafe {
        SetWindowTextW(state.status_label, text.as_ptr());
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
    if msg == WM_NCLBUTTONUP {
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
    let file_roots = settings
        .index
        .roots
        .iter()
        .filter_map(|root| expand_path(root).ok())
        .collect();
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
        status_label: std::ptr::null_mut(),
        results_list: std::ptr::null_mut(),
        pending_label: std::ptr::null_mut(),
        busy: false,
        searching: false,
        cancel: Arc::new(AtomicBool::new(false)),
        pending_id: None,
        search_results: Vec::new(),
        file_roots,
        last_pending_refresh: Instant::now(),
        last_status_refresh: Instant::now(),
        glossary_window: std::ptr::null_mut(),
    });
    let instance = unsafe { GetModuleHandleW(std::ptr::null()) };
    register_class(CLASS, window_proc, instance)?;
    register_class(GLOSSARY_CLASS, glossary_window_proc, instance)?;
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
            (screen_h - 690).max(0),
            760,
            650,
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

#[cfg(test)]
mod tests {
    use super::transcript_suffix;

    #[test]
    fn transcript_trim_keeps_utf16_surrogate_pairs_intact() {
        assert_eq!(transcript_suffix("abc🧡def", 5), "🧡def");
        assert_eq!(transcript_suffix("abc🧡def", 6), "c🧡def");
    }

    #[test]
    fn transcript_trim_bounds_large_text_without_splitting_utf8() {
        assert_eq!(transcript_suffix("first\r\nlast", 4), "last");
        assert_eq!(transcript_suffix("short", 100), "short");
    }
}
