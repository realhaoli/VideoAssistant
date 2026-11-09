"""Aggregate independent output branches without letting a fast one report 99%."""
import threading


class OutputProgress:
    def __init__(self, store, job_id, modes):
        self.store, self.job_id = store, job_id
        self.lock = threading.RLock()
        self.groups = {'report':set(modes) & {'illustrated','text','mindmap'},
                       'video':set(modes) & {'download'}}
        self.weights = {key:(4 if key == 'report' else 2) for key,value in self.groups.items() if value}
        self.values = {key:0 for key in self.weights}
        for mode in modes:
            store.update_output(job_id, mode, status='pending', progress=0, message='等待处理')

    def total(self):
        return 55 + 44 * sum(self.values[k] * weight for k,weight in self.weights.items()) / max(1,sum(self.weights.values()))

    def for_branch(self, branch, modes=None):
        selected = self.groups[branch] if modes is None else self.groups[branch] & modes
        def progress(stage, percent, message, stage_percent=None):
            with self.lock:
                start, span = (60,33) if branch == 'report' else (93,5)
                self.values[branch] = max(self.values[branch], min(.99, max(0,(percent-start)/span)))
                states = self.store.get(self.job_id).get('output_states',{})
                for mode in selected:
                    if states.get(mode,{}).get('status') not in {'completed','failed'}:
                        self.store.update_output(self.job_id, mode, status='running', progress=round(self.values[branch]*100),
                                                 message=message, stage_progress=stage_percent)
                self.store.progress(self.job_id, stage, self.total(), message, stage_progress=stage_percent)
        return progress

    def done(self, mode):
        self._finish(mode, 'completed', None)

    def failed(self, mode, error):
        self._finish(mode, 'failed', error)

    def _finish(self, mode, status, error):
        with self.lock:
            previous = self.store.get(self.job_id).get('output_states',{}).get(mode,{})
            if previous.get('status') == 'completed':
                return
            self.store.update_output(self.job_id, mode, status=status, progress=100 if status=='completed' else previous.get('progress',0),
                                     stage_progress=None, message='已完成' if status == 'completed' else '处理失败', error=error)
            states = self.store.get(self.job_id).get('output_states',{})
            for branch, modes in self.groups.items():
                if modes and all(states.get(mode,{}).get('status') in {'completed','failed'} for mode in modes):
                    self.values[branch] = 1
            self.store.update(self.job_id, progress=self.total())
