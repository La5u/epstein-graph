"""Pan/zoom frame rate of the viewer (output/graph.html) on the real GPU, headless.

Drives the camera along a fixed path (circle pan while zooming 1x -> 4x -> 1x)
and reports frame times between requestAnimationFrame callbacks. --all shows
every node and link first; --uncapped lifts the 60 fps display cap to expose
headroom. Check the reported "gpu" is not SwiftShader (software GL).
"""
import argparse
import json
import shutil
import statistics
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
GPU = ['--enable-gpu', '--use-angle=gl-egl', '--ignore-gpu-blocklist']
RUN = '''(frames)=>new Promise(resolve=>{
  renderer.fit();const base={...renderer.camera},extent=Math.min(renderer.width,renderer.height)/base.scale;
  const times=[];let last=null,i=0;
  function step(now){
    if(last!==null)times.push(now-last);
    last=now;
    if(i>=frames){resolve(times);return;}
    const t=i/frames,a=2*Math.PI*t;i++;
    renderer.setCamera({x:base.x+0.15*extent*Math.cos(a),y:base.y+0.15*extent*Math.sin(a),scale:base.scale*(1+3*Math.sin(Math.PI*t))});
    requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
})'''
SHOW_ALL = '''()=>{
  for(const box of document.querySelectorAll('[data-hide],[data-status]')){box.checked=false;box.onchange();}
  state.hiddenTypes.clear();refreshTypeButtons();update();
}'''


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--html', default=str(ROOT / 'output/graph.html'))
    parser.add_argument('--frames', type=int, default=360)
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--uncapped', action='store_true')
    args = parser.parse_args()
    flags = GPU + (['--disable-gpu-vsync', '--disable-frame-rate-limit'] if args.uncapped else [])
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which('chromium-browser') or shutil.which('chromium'), headless=True, args=flags)
        page = browser.new_page(viewport={'width': 1600, 'height': 1000})
        page.goto(Path(args.html).resolve().as_uri())
        page.wait_for_function("document.getElementById('status').textContent.includes('layout frozen') && !state.spreading", timeout=120000)
        if args.all:
            page.evaluate(SHOW_ALL)
        gpu = page.evaluate("()=>{const gl=renderer.gl,e=gl.getExtension('WEBGL_debug_renderer_info');return e?gl.getParameter(e.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER);}")
        shown = page.evaluate("[data.nodes.filter(n=>!n.hidden).length, data.edges.filter(e=>!e.hidden).length]")
        times = sorted(page.evaluate(RUN, args.frames)[5:])
        browser.close()
    mean = statistics.fmean(times)
    print(json.dumps({'gpu': gpu, 'nodes': shown[0], 'edges': shown[1], 'mean_fps': round(1000 / mean, 1), 'mean_ms': round(mean, 2),
                      'p95_ms': round(times[int(len(times) * 0.95)], 2), 'worst_ms': round(times[-1], 2)}, indent=2))


if __name__ == '__main__':
    main()
