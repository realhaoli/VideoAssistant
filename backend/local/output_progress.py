"""Estimate work completed, counting shared phases only once."""

PHASE_WEIGHTS = {
    'inspect': 2, 'download': 28, 'audio': 2, 'whisper_model': 3,
    'transcribe': 42, 'clean': 18, 'translate': 22, 'transcript': 2,
    'subtitles': 1, 'chapters': 18, 'report': 38, 'overview': 12,
    'illustrate': 32, 'mindmap': 15, 'export_video': 18,
    'write_text': 2, 'write_illustrated': 2, 'export': 1,
}


class OutputProgress:
    def __init__(self, store, job_id, modes):
        self.store = store
        self.job_id = job_id
        self.modes = set(modes)
        reports = self.modes & {'text', 'illustrated', 'mindmap'}
        self.phases = {}
        self.values = {}
        self.weights = PHASE_WEIGHTS.copy()
        self.configure(
            inspect=self.modes, transcript=self.modes, subtitles={'subtitles'},
            chapters=reports, report=reports, overview=reports - {'mindmap'},
            illustrate={'illustrated'}, mindmap={'mindmap'}, export_video={'download'},
            write_text={'text'}, write_illustrated={'illustrated'}, export=self.modes,
        )

    def configure(self, **phases):
        with self.store.lock:
            for phase, modes in phases.items():
                self.phases[phase] = self.modes & modes

    def complete(self, *phases):
        with self.store.lock:
            for phase in phases:
                self.values[phase] = 1

    def reset(self, phase):
        with self.store.lock:
            self.values.pop(phase, None)

    def total(self, mode=None):
        phases = [phase for phase, modes in self.phases.items()
                  if modes and (mode is None or phase != 'export' and mode in modes)]
        weight = sum(self.weights[phase] for phase in phases)
        completed = sum(self.weights[phase] * self.values.get(phase, 0) for phase in phases)
        return completed * 100 / weight if weight else 0

    def __call__(self, stage, message, stage_percent=None, *, phase=None, fraction=None, visible=None):
        phase = phase or stage
        if fraction is None:
            fraction = (stage_percent or 0) / 100
        with self.store.lock:
            if phase in self.phases:
                self.values[phase] = max(self.values.get(phase, 0), min(fraction, 1))
            selected = self.phases.get(phase, set()) if visible is None else visible
            updates = {}
            for mode, state in self.store.output_states(self.job_id).items():
                if mode not in self.modes or state.get('status') in {'completed', 'failed'}:
                    continue
                percent = min(99, round(self.total(mode)))
                fields = {}
                if percent > state.get('progress', 0):
                    fields['progress'] = percent
                if mode in selected:
                    fields.update(status='running', stage=stage, message=message, stage_progress=stage_percent)
                if fields:
                    updates[mode] = fields
            self.store.progress(self.job_id, stage, self.total(), message, stage_progress=stage_percent,
                                output_updates=updates)

    def queue(self, mode):
        self.store.update_output(self.job_id, mode, status='queued', stage='outputs_wait',
                                 stage_progress=None, message='等待产物处理', error=None)

    def done(self, mode):
        with self.store.lock:
            if self.store.output_states(self.job_id).get(mode, {}).get('status') != 'completed':
                self.store.update_output(self.job_id, mode, status='completed', progress=100,
                                         stage_progress=None, message='已完成', error=None)

    def failed(self, mode, error):
        with self.store.lock:
            if self.store.output_states(self.job_id).get(mode, {}).get('status') != 'failed':
                self.store.update_output(self.job_id, mode, status='failed', stage_progress=None,
                                         message='处理失败', error=error)
