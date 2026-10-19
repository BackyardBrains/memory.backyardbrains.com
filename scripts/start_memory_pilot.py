"""Start the authorized fourteen-day pilot once; reruns preserve its dates."""
import json
import subprocess
from datetime import datetime,timedelta,timezone
from pathlib import Path


def start():
    directory=Path('/var/lib/byb-memory-health');directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=directory/'pilot.json'
    if path.exists():
        config=json.loads(path.read_text())
        if config.get('pilot_id')!='watson-usefulness-20261015':
            raise RuntimeError('Another pilot is configured; preserve its settings')
    else:
        now=datetime.now(timezone.utc).replace(microsecond=0)
        config=dict(pilot_id='watson-usefulness-20261015',starts_at=now.isoformat(),
                    ends_at=(now+timedelta(days=14)).isoformat(),
                    projects=['spikerecorder','byb-shipping','shared-agent-instructions'],
                    targets=dict(recall_at_5=0.9,critical_retrieval_failures=0,abstention_accuracy=1.0),
                    behavior_assessment='Review actual answers/actions and observed misses; fetching is not compliance.')
        temp=directory/'pilot.tmp';temp.write_text(json.dumps(config,indent=2)+'\n');temp.chmod(0o600);temp.replace(path)
    end=datetime.fromisoformat(config['ends_at']).astimezone(timezone.utc)
    timer='''[Unit]
Description=Close the fourteen-day Watson memory usefulness pilot

[Timer]
OnCalendar=%s UTC
Persistent=true
Unit=byb-memory-quality.service

[Install]
WantedBy=timers.target
''' % end.strftime('%Y-%m-%d %H:%M:%S')
    Path('/etc/systemd/system/byb-memory-pilot-close.timer').write_text(timer)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','byb-memory-pilot-close.timer'],check=True)
    return config


if __name__=='__main__':print(json.dumps(start()))
