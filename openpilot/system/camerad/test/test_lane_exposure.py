"""Native PC contracts for optional lane-weighted exposure, not road validation."""
from pathlib import Path
import subprocess


def test_native_lane_projection_weights_fallback_and_metering(tmp_path):
  root = Path(__file__).resolve().parents[3]
  source = r'''
#include "system/camerad/cameras/model_fov_meter.h"
#include <cassert>
#include <limits>
#include <vector>
int main() {
  ExposureLaneInput input;
  input.count=33; input.probability={.99f,.99f}; input.deviation={.1f,.1f};
  for(int i=0;i<33;++i) {
    input.left[i]={float(i*2),-1.8f,1.4f};
    input.right[i]={float(i*2),1.8f,1.4f};
  }
  for(int wide=0;wide<2;++wide) for(int selection=0;selection<5;++selection) {
    auto c=exposure_lane_corridor(input,wide,selection); assert(c.valid);
    const double focal=wide ? (selection ? 256/std::tan((60+5*selection)*std::acos(-1.)/360) :455) :910;
    const double cy=wide ?151.8:47.6;
    int count=0;
    for(int row=0;row<36;++row) if(c.right[row]>c.left[row]) {
      const double v=255.*(row+.5)/36;
      const double x=focal*1.4/(v-cy);
      assert(std::abs(c.left[row]-std::max(0.,256-1.8*focal/x))<.002);
      assert(std::abs(c.right[row]-std::min(511.,256+1.8*focal/x))<.002);++count;
    }
    assert(count>=6);
  }
  for(int fault=0;fault<10;++fault) {
    auto bad=input;
    if(fault==0)bad.count=34;
    if(fault==1)bad.probability[0]=.79;
    if(fault==2)bad.deviation[1]=.31;
    if(fault==3)bad.left[4].z=std::nanf("");
    if(fault==4)bad.right[4].x=bad.right[3].x;
    if(fault==5)bad.right[4].y=bad.left[4].y;
    if(fault==6)bad.left[4].z=-.1;
    if(fault==7)bad.left[4].x=bad.right[4].x+1;
    if(fault==8)bad.probability[0]=std::nanf("");
    if(fault==9)bad.left[10].z=4;
    assert(!exposure_lane_corridor(bad,true,4).valid);
  }
  auto far=input;far.left[32].z=-10;far.right[32].z=-10;
  assert(exposure_lane_corridor(far,true,4).valid); // Unused >60m predictions cannot veto the near corridor.
  auto straight=exposure_lane_corridor(input,true,4);
  for(int i=0;i<33;++i) { const float curve=.0008f*input.left[i].x*input.left[i].x;
    input.left[i].y+=curve; input.right[i].y+=curve; }
  auto curve=exposure_lane_corridor(input,true,4); assert(curve.valid);
  bool shifted=false;for(int row=0;row<36;++row) shifted|=curve.left[row]>straight.left[row]+1;
  assert(shifted);
  ExposureLaneWeights weights;
  uint64_t now=1000000000ULL;
  for(int frame=0;frame<60;++frame) {
    now+=50000000ULL; straight.valid_until=now+250000000ULL;
    weights.update(&straight,now);
    if(frame<10)assert(!weights.active());
  }
  assert(weights.active());
  auto previous=weights;
  now+=50000000ULL;curve.valid_until=now+250000000ULL; weights.update(&curve,now);
  for(int row=0;row<36;++row) if(previous.strength[row]>0 && curve.right[row]>curve.left[row]) {
    assert(std::abs(weights.left[row]-previous.left[row])<=3.201);
    assert(std::abs(weights.right[row]-previous.right[row])<=3.201);
  }
  for(int i=0;i<2304;++i)assert(weights.extra(i)>=0 && weights.extra(i)<=16);
  // A repeated old source cannot maintain freshness, and does not need a mode switch.
  const uint64_t deadline=curve.valid_until;
  for(int frame=0;frame<30;++frame){now+=50000000ULL;weights.update(&curve,now);}
  assert(now>deadline+1000000000ULL && !weights.active());
  now+=50000000ULL;curve.valid_until=now+250000000ULL;weights.update(&curve,now);
  assert(!weights.active());
  weights.update(nullptr,now);assert(!weights.active());
  // Real cached sampler on a nonuniform frame: lane delta is bounded relative
  // to each explicitly chosen base, and invalid lanes converge exactly to it.
  std::vector<uint8_t> pixels(1928*1208);
  for(int y=0;y<1208;++y)for(int x=0;x<1928;++x)pixels[y*1928+x]=20+(1208-y)*180/1208+(x%100)/10;
  for(bool assisted:{false,true}) {
    ModelFovMeter lane,base;
    assert(lane.configure(1928,1208,true,{},4));assert(base.configure(1928,1208,true,{},4));
    now=1000000000ULL;float previous_bias=0;bool difference=false;
    for(int frame=0;frame<160;++frame) {
      now+=50000000ULL; straight.valid_until=now+250000000ULL;
      assert(lane.measure(pixels.data(),pixels.size(),assisted,&straight,now));
      assert(base.measure(pixels.data(),pixels.size(),assisted));
      const float lg=lane.assisted_grey(true,now), bg=base.assisted_grey(assisted,now);
      const float bias=std::log2(lane.grey/lg);
      assert(std::abs(bias)<=.3751 && std::abs(bias-previous_bias)<=.02001);previous_bias=bias;
      assert(std::abs(std::log2(bg/lg))<=.1251);difference|=std::abs(lg-bg)>.0001;
    }
    assert(difference);
    straight.valid=false;
    for(int frame=0;frame<600;++frame) {
      now+=50000000ULL;
      lane.measure(pixels.data(),pixels.size(),assisted,&straight,now);
      base.measure(pixels.data(),pixels.size(),assisted);
      const float lg=lane.assisted_grey(true,now),bg=base.assisted_grey(assisted,now);
      if(frame==599)assert(std::abs(lg-bg)<1e-6);
    }
    straight.valid=true;
    lane.measure(pixels.data(),pixels.size(),false,nullptr,now+50000000ULL);
    assert(lane.assisted_grey(false,now+50000000ULL,.4f)==.4f);
  }
}
'''
  cpp = tmp_path / 'lane.cc'
  cpp.write_text(source)
  binary = tmp_path / 'lane'
  result = subprocess.run(['g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-I',str(root),str(cpp),'-o',str(binary)],capture_output=True)
  assert result.returncode == 0, result.stderr.decode()
  result = subprocess.run([str(binary)],capture_output=True,timeout=15)
  assert result.returncode == 0, result.stderr.decode()
