//! Small wake/idle coordinator for the model-backed assistant.
use crate::agent::TurnMetrics;
use crate::agent::ollama_runtime::{AgentError, AgentTools, BotRuntime, OllamaTransport};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, RecvTimeoutError, Sender},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

/// User-visible lifecycle reported to the avatar.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LifecycleState {
    Sleeping,
    Active,
}

/// The minimum operations needed by the idle supervisor.
pub trait ManagedAssistant: Send + 'static {
    fn ask_streaming(
        &mut self,
        prompt: &str,
        on_delta: &mut dyn FnMut(&str) -> bool,
    ) -> Result<String, AgentError>;
    fn ask_streaming_profiled(
        &mut self,
        prompt: &str,
        on_delta: &mut dyn FnMut(&str) -> bool,
    ) -> Result<(String, TurnMetrics), AgentError>;
    fn ask_streaming_profiled_cancellable(
        &mut self,
        prompt: &str,
        on_delta: &mut dyn FnMut(&str) -> bool,
        is_cancelled: &mut dyn FnMut() -> bool,
    ) -> Result<(String, TurnMetrics), AgentError> {
        if is_cancelled() {
            return Err(AgentError::Cancelled);
        }
        self.ask_streaming_profiled(prompt, on_delta)
    }
    fn sleep(&mut self);
}

impl<T, A> ManagedAssistant for BotRuntime<T, A>
where
    T: OllamaTransport + Send + 'static,
    A: AgentTools + Send + 'static,
{
    fn ask_streaming(
        &mut self,
        prompt: &str,
        on_delta: &mut dyn FnMut(&str) -> bool,
    ) -> Result<String, AgentError> {
        BotRuntime::ask_streaming(self, prompt, on_delta)
    }

    fn ask_streaming_profiled(
        &mut self,
        prompt: &str,
        on_delta: &mut dyn FnMut(&str) -> bool,
    ) -> Result<(String, TurnMetrics), AgentError> {
        BotRuntime::ask_streaming_profiled(self, prompt, on_delta)
    }

    fn ask_streaming_profiled_cancellable(
        &mut self,
        prompt: &str,
        on_delta: &mut dyn FnMut(&str) -> bool,
        is_cancelled: &mut dyn FnMut() -> bool,
    ) -> Result<(String, TurnMetrics), AgentError> {
        BotRuntime::ask_streaming_profiled_cancellable(self, prompt, on_delta, is_cancelled)
    }

    fn sleep(&mut self) {
        BotRuntime::sleep(self);
    }
}

enum WorkerMessage {
    Stop,
}

/// Runs with dormant resources until activated and unloads the model after idle timeout.
///
/// The assistant object can contain configuration and lightweight clients while asleep;
/// its model-backed `ask_streaming` and `sleep` methods control heavyweight resources.
pub struct LifecycleManager<A: ManagedAssistant> {
    assistant: Arc<Mutex<A>>,
    state: Arc<AtomicBool>,
    last_activity: Arc<Mutex<Instant>>,
    idle_timeout: Duration,
    stop: Sender<WorkerMessage>,
    worker: Option<JoinHandle<()>>,
}

impl<A: ManagedAssistant> LifecycleManager<A> {
    /// Start the low-cost idle watcher. No inference or model loading occurs here.
    pub fn new(assistant: A, idle_timeout: Duration) -> std::io::Result<Self> {
        let assistant = Arc::new(Mutex::new(assistant));
        let state = Arc::new(AtomicBool::new(false));
        let last_activity = Arc::new(Mutex::new(Instant::now()));
        let timeout = idle_timeout.max(Duration::from_millis(1));
        let poll_interval = timeout
            .min(Duration::from_secs(1))
            .max(Duration::from_millis(25));
        let (stop, receiver) = mpsc::channel();
        let worker_assistant = Arc::clone(&assistant);
        let worker_state = Arc::clone(&state);
        let worker_activity = Arc::clone(&last_activity);
        let worker = thread::Builder::new()
            .name("filebot-idle-unloader".into())
            .spawn(move || {
                loop {
                    match receiver.recv_timeout(poll_interval) {
                        Ok(WorkerMessage::Stop) | Err(RecvTimeoutError::Disconnected) => break,
                        Err(RecvTimeoutError::Timeout) => {}
                    }
                    if !worker_state.load(Ordering::Acquire) {
                        continue;
                    }
                    let is_idle = worker_activity
                        .lock()
                        .is_ok_and(|last| last.elapsed() >= timeout);
                    if !is_idle {
                        continue;
                    }
                    let Ok(mut assistant) = worker_assistant.lock() else {
                        continue;
                    };
                    let still_idle = worker_activity
                        .lock()
                        .is_ok_and(|last| last.elapsed() >= timeout);
                    if still_idle && worker_state.load(Ordering::Acquire) {
                        assistant.sleep();
                        worker_state.store(false, Ordering::Release);
                    }
                }
            })?;
        Ok(Self {
            assistant,
            state,
            last_activity,
            idle_timeout: timeout,
            stop,
            worker: Some(worker),
        })
    }

    /// Mark the assistant awake after a hotkey or avatar activation.
    pub fn wake(&self) {
        if let Ok(mut last) = self.last_activity.lock() {
            *last = Instant::now();
        }
        self.state.store(true, Ordering::Release);
    }

    /// Run one streamed turn and restart the idle timer on completion or failure.
    pub fn ask_streaming(
        &self,
        prompt: &str,
        on_delta: impl FnMut(&str) -> bool,
    ) -> Result<String, AgentError> {
        self.ask_streaming_profiled(prompt, on_delta)
            .map(|(answer, _metrics)| answer)
    }

    /// Run a turn and return timing data without retaining prompt or response text.
    pub fn ask_streaming_profiled(
        &self,
        prompt: &str,
        on_delta: impl FnMut(&str) -> bool,
    ) -> Result<(String, TurnMetrics), AgentError> {
        self.ask_streaming_profiled_cancellable(prompt, on_delta, || false)
    }

    /// Run with a cancellation probe checked during model preflight/startup and turn boundaries.
    pub fn ask_streaming_profiled_cancellable(
        &self,
        prompt: &str,
        mut on_delta: impl FnMut(&str) -> bool,
        mut is_cancelled: impl FnMut() -> bool,
    ) -> Result<(String, TurnMetrics), AgentError> {
        self.wake();
        let result = {
            let mut assistant = self
                .assistant
                .lock()
                .map_err(|_| AgentError::Response("assistant lock was poisoned".into()))?;
            assistant.ask_streaming_profiled_cancellable(prompt, &mut on_delta, &mut is_cancelled)
        };
        self.wake();
        result
    }

    /// Force model and embedding resources to sleep immediately.
    pub fn sleep(&self) -> Result<(), AgentError> {
        let mut assistant = self
            .assistant
            .lock()
            .map_err(|_| AgentError::Response("assistant lock was poisoned".into()))?;
        assistant.sleep();
        self.state.store(false, Ordering::Release);
        self.touch();
        Ok(())
    }

    /// Current avatar state.
    pub fn state(&self) -> LifecycleState {
        if self.state.load(Ordering::Acquire) {
            LifecycleState::Active
        } else {
            LifecycleState::Sleeping
        }
    }

    /// Configured idle interval for UI status and diagnostics.
    pub fn idle_timeout(&self) -> Duration {
        self.idle_timeout
    }

    fn touch(&self) {
        if let Ok(mut last) = self.last_activity.lock() {
            *last = Instant::now();
        }
    }
}

impl<A: ManagedAssistant> Drop for LifecycleManager<A> {
    fn drop(&mut self) {
        let _ = self.stop.send(WorkerMessage::Stop);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
        if let Ok(mut assistant) = self.assistant.lock() {
            assistant.sleep();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    #[derive(Clone, Default)]
    struct FakeAssistant {
        asks: Arc<AtomicUsize>,
        sleeps: Arc<AtomicUsize>,
    }

    impl ManagedAssistant for FakeAssistant {
        fn ask_streaming(
            &mut self,
            prompt: &str,
            on_delta: &mut dyn FnMut(&str) -> bool,
        ) -> Result<String, AgentError> {
            self.asks.fetch_add(1, Ordering::Relaxed);
            let _ = on_delta(prompt);
            Ok("ok".into())
        }
        fn ask_streaming_profiled(
            &mut self,
            prompt: &str,
            on_delta: &mut dyn FnMut(&str) -> bool,
        ) -> Result<(String, TurnMetrics), AgentError> {
            let answer = self.ask_streaming(prompt, on_delta)?;
            Ok((
                answer,
                TurnMetrics {
                    total: Duration::from_millis(1),
                    model_preflight: Duration::from_millis(1),
                    time_to_first_token: Some(Duration::from_millis(1)),
                    response_chars: 2,
                },
            ))
        }
        fn sleep(&mut self) {
            self.sleeps.fetch_add(1, Ordering::Relaxed);
        }
    }

    #[test]
    fn stays_dormant_until_a_turn_then_unloads_after_idle_timeout() {
        let fake = FakeAssistant::default();
        let asks = Arc::clone(&fake.asks);
        let sleeps = Arc::clone(&fake.sleeps);
        let manager = LifecycleManager::new(fake, Duration::from_millis(100)).unwrap();
        assert_eq!(manager.state(), LifecycleState::Sleeping);
        assert_eq!(asks.load(Ordering::Relaxed), 0);
        assert_eq!(manager.ask_streaming("hello", |_| true).unwrap(), "ok");
        assert_eq!(manager.state(), LifecycleState::Active);
        let deadline = Instant::now() + Duration::from_secs(2);
        while manager.state() != LifecycleState::Sleeping && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(manager.state(), LifecycleState::Sleeping);
        assert_eq!(sleeps.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn explicit_sleep_unloads_immediately_and_drop_releases_again_safely() {
        let fake = FakeAssistant::default();
        let sleeps = Arc::clone(&fake.sleeps);
        let manager = LifecycleManager::new(fake, Duration::from_secs(30)).unwrap();
        manager.wake();
        manager.sleep().unwrap();
        assert_eq!(manager.state(), LifecycleState::Sleeping);
        assert_eq!(sleeps.load(Ordering::Relaxed), 1);
    }
}
