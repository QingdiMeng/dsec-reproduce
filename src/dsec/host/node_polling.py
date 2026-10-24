"""Bounded probes over a changing route directory; no thread per new Node."""
from concurrent.futures import ThreadPoolExecutor


def poll_nodes(watcher, stop, interval, *, workers=16):
    if type(workers) is not int or not 1 <= workers <= 128:
        raise ValueError('Probe concurrency must be in [1, 128]')
    pending, position = {}, 0
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='dsec-probe') as pool:
        while not stop.is_set():
            for node,future in tuple(pending.items()):
                if future.done():
                    future.result()
                    del pending[node]
            try:
                nodes = sorted(watcher.probes)
            except Exception:
                # Quorum/directory loss never grants admission or releases
                # work. Existing derived observations expire normally.
                stop.wait(interval)
                continue
            if nodes:
                start=position % len(nodes)
                for offset in range(len(nodes)):
                    if len(pending)>=workers:break
                    index=(start+offset) % len(nodes)
                    node=nodes[index]
                    position=(index+1) % len(nodes)
                    if node not in pending:
                        pending[node]=pool.submit(watcher.probe,node)
            stop.wait(interval)
