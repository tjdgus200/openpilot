"""Native contracts for geometry-driven whole-box translation, not road validation."""
from pathlib import Path
import subprocess


def test_native_geometry_box_motion_sampling_and_fallback(tmp_path):
  root = Path(__file__).resolve().parents[3]
  source = r'''
#include "system/camerad/cameras/model_fov_meter.h"
#include <cassert>
#include <vector>
int main() {
  ExposureLaneInput input;
  input.count=33; input.probability={.99f,.99f}; input.deviation={.1f,.1f};
  for(int i=0;i<33;++i) {
    input.left[i]={float(i*2),-1.8f,1.4f}; input.right[i]={float(i*2),1.8f,1.4f};
  }
  // Geometry, not luminance, sets the box center. Both lenses aim at the same road.
  for(int wide=0;wide<2;++wide) for(int selection=0;selection<5;++selection) {
    auto target=exposure_box_target(input,wide,selection,{});assert(target.valid);
    assert(std::abs(target.center_x-964)<.001);
    auto turn=input;
    for(int i=0;i<33;++i){float offset=.0008f*turn.left[i].x*turn.left[i].x;turn.left[i].y+=offset;turn.right[i].y+=offset;}
    auto right=exposure_box_target(turn,wide,selection,{});assert(right.valid&&right.center_x>964);
    const double expected=964+(wide?567:2648)*(.0008*(15.+25.+35.)/3);
    assert(std::abs(right.center_x-expected)<.15); // linear interpolation of a quadratic at integer sample positions
    for(int i=0;i<33;++i){turn.left[i].y=-turn.left[i].y;turn.right[i].y=-turn.right[i].y;std::swap(turn.left[i],turn.right[i]);}
    auto left=exposure_box_target(turn,wide,selection,{});assert(left.valid&&left.center_x<964);
  }
  for(int fault=0;fault<9;++fault) {
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
    assert(!exposure_box_target(bad,true,4,{}).valid);
  }
  assert(!exposure_box_target(input,true,4,{0,std::nan(""),0}).valid);
  auto far=input;far.left[32].z=-10;far.right[32].z=-10;
  assert(exposure_box_target(far,true,4,{}).valid);
  ExposureBoxMotion motion;
  ExposureBoxTarget target;target.valid=true;target.center_x=1100;
  uint64_t now=1000000000ULL;
  for(int frame=0;frame<60;++frame) {
    now+=50000000ULL;target.valid_until=now+250000000ULL;
    const float before=motion.offset;motion.update(&target,now,964,1);
    if(frame<10)assert(motion.offset==0);
    assert(std::abs(motion.offset-before)<=3.201&&std::abs(motion.offset)<=64.001);
  }
  assert(motion.offset==64);
  // Repeated old input cannot maintain position; return motion is bounded too.
  for(int frame=0;frame<40;++frame){now+=50000000ULL;float old=motion.offset;motion.update(&target,now,964,1);assert(std::abs(motion.offset-old)<=3.201);}
  assert(motion.offset==0);
  target.valid_until=now+250000000ULL;now+=50000000ULL;motion.update(&target,now,964,1);assert(motion.offset==0);
  motion.update(nullptr,now,964,1);assert(motion.offset==0);
  // Full-size translated rectangles: identical corner translation, constant
  // width/height, zero vertical motion and no per-lane-pixel weights.
  std::vector<uint8_t> pixels(2048*1208,255);
  for(int y=0;y<1208;++y)for(int x=0;x<1928;++x)pixels[y*2048+x]=30+x/12;
  for(bool wide:{false,true}) for(bool assisted:{false,true}) for(bool stock:{false,true}) {
    if(stock&&assisted)continue;
    ModelFovMeter box,base;
    const ModelFovRegion region=stock?ModelFovRegion{97,160,1734,986}:ModelFovRegion{};
    assert(box.configure(1928,1208,wide,{},4,2048,region));
    assert(base.configure(1928,1208,wide,{},4,2048,region));
    const auto original=box.polygon;
    now=1000000000ULL;float previous_bias=0;bool changed=false;
    for(int frame=0;frame<160;++frame){
      now+=50000000ULL;target.valid_until=now+250000000ULL;
      assert(box.measure(pixels.data(),pixels.size(),assisted,&target,now));
      assert(base.measure(pixels.data(),pixels.size(),assisted));
      const float a=box.assisted_grey(true,now),b=base.assisted_grey(assisted,now);
      const float bias=std::log2(box.grey/a);
      assert(std::abs(bias)<=.3751&&std::abs(bias-previous_bias)<=.02001);previous_bias=bias;
      assert(std::abs(std::log2(b/a))<=.1251);changed|=std::abs(a-b)>.0001;
      const float dx=box.polygon[0]-original[0];
      for(int corner=0;corner<4;++corner){assert(std::abs(box.polygon[corner*2]-original[corner*2]-dx)<.001);assert(box.polygon[corner*2+1]==original[corner*2+1]);}
    }
    assert(changed&&box.polygon[0]>original[0]);
    target.valid=false;
    for(int frame=0;frame<600;++frame){now+=50000000ULL;
      box.measure(pixels.data(),pixels.size(),assisted,&target,now);base.measure(pixels.data(),pixels.size(),assisted);
      const float a=box.assisted_grey(true,now),b=base.assisted_grey(assisted,now);
      if(frame==599){assert(box.polygon==original);assert(std::abs(a-b)<1e-6);}
    }
    target.valid=true;
    box.measure(pixels.data(),pixels.size(),false,nullptr,now+50000000ULL);
    assert(box.assisted_grey(false,now+50000000ULL,.4f)==.4f);
  }
}
'''
  cpp = tmp_path / 'box.cc'
  cpp.write_text(source)
  binary = tmp_path / 'box'
  result = subprocess.run(['g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-I',str(root),str(cpp),'-o',str(binary)],capture_output=True)
  assert result.returncode == 0, result.stderr.decode()
  result = subprocess.run([str(binary)],capture_output=True,timeout=15)
  assert result.returncode == 0, result.stderr.decode()
