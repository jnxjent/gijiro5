# timer.py（kowake.py と同じディレクトリでOK）
import time

class Timer:
    def __init__(self):
        self.logs = []
        self.start_time = time.time()

    def mark(self, label):
        now = time.time()
        self.logs.append((label, now))
        print(f"[TIMER] {label}: {now - self.start_time:.3f} 秒経過")

    def report(self):
        print("[TIMER] ── 処理時間レポート ──")
        for i in range(1, len(self.logs)):
            l1, t1 = self.logs[i-1]
            l2, t2 = self.logs[i]
            print(f"  {l1} → {l2}: {t2 - t1:.3f} 秒")
        print("[TIMER] ──────────────────────")
