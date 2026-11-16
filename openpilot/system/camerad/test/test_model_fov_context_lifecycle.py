"""Production exposure context + native msgq, anonymous PC queues only.

Params/cereal are deterministic boundaries, not a full camerad/device test.
"""
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[4]

NATIVE = r'''
#include <array>
#include <atomic>
#include <csignal>
#include <iostream>
#include "msgq/msgq.h"
struct QueueFixture {
  std::atomic<uint64_t> readers{0}, write{0}, uid{0};
  std::array<std::atomic<uint64_t>,NUM_READERS> rp{},rv{},ru{};
  std::vector<char> data=std::vector<char>(DEFAULT_SEGMENT_SIZE);
  msgq_queue_t queue(){
    msgq_queue_t q{};q.num_readers=&readers;q.write_pointer=&write;q.write_uid=&uid;
    for(int i=0;i<NUM_READERS;++i){q.read_pointers[i]=&rp[i];q.read_valids[i]=&rv[i];q.read_uids[i]=&ru[i];}
    q.data=data.data();q.size=data.size();q.reader_id=-1;q.mmap_p=nullptr;q.read_conflate=true;return q;
  }
};
std::map<std::string,QueueFixture> queues;
std::map<std::string,int> created,polled;
int masters=0,live_subs=0;
uint32_t receive(msgq_queue_t& q){
  msgq_msg_t m{};int n=msgq_msg_recv(&m,&q);if(!n)return 0;assert(n==4);
  uint32_t v;memcpy(&v,m.data,4);msgq_msg_close(&m);return v;
}
void send(msgq_queue_t& q,uint32_t v){
  msgq_msg_t m{};assert(msgq_msg_init_data(&m,reinterpret_cast<char*>(&v),sizeof(v))==0);
  assert(msgq_msg_send(&m,&q)==sizeof(v));msgq_msg_close(&m);
}
'''

SCENARIOS = r'''
int main(int argc,char** argv){
  assert(argc==2);std::signal(SIGUSR2,SIG_IGN);const std::string mode=argv[1];
  std::vector<std::string> names={"liveCalibration","deviceState","modelV2"};
  std::map<std::string,msgq_queue_t> publishers;
  std::map<std::string,std::array<msgq_queue_t,3>> witnesses;
  for(const auto& name:names){
    auto& f=queues[name];publishers[name]=f.queue();msgq_init_publisher(&publishers[name]);
    witnesses[name]={f.queue(),f.queue(),f.queue()};
    for(auto& q:witnesses[name])msgq_init_subscriber(&q);
  }
  int losses=0,active_slots=0,guard_checks=0,stress_masters=0,stress_cal=0,stress_model=0;
  std::map<std::string,int> lost_by_topic;
  {
    ModelFovContext context;
    // A never-enabled feature allocates no subscribers and polls no topics.
    values={{"UseWideCamera","1"},{"CameraYawTrimDeg","0"},{"CarParams","12345678"}};
    for(int i=0;i<5;++i){clock_now+=300000000ULL;context.update(true);assert(!context.ready);}
    assert(masters==0 && live_subs==0);
    values["Pv5ModelFovExposure"]="1";clock_now+=300000000ULL;cal_time=dev_time=clock_now;context.update(true);assert(context.ready);
    const int count=mode=="audit100"?100:1000;
    for(int i=1;i<=count;++i){
      clock_now+=300000000ULL;cal_time=dev_time=clock_now;values["Pv5ModelFovExposure"]="1";
      values["UseWideCamera"]="1";values["Pv5RoadLaneExposure"]="0";env_ctrl_exp_from_params=false;
      bool dual=true,enabled=true;
      if(mode=="lane" || mode=="audit100") values["Pv5RoadLaneExposure"]=i%2?"1":"0";
      if(mode=="power"){values["Pv5ModelFovExposure"]=i%2?"1":"0";enabled=i%2;}
      if(mode=="mixed"){
        switch(i%6){
          case 0: values["Pv5RoadLaneExposure"]="1";break;
          case 1: break;
          case 2: values["Pv5ModelFovExposure"]="0";enabled=false;break;
          case 3: values["UseWideCamera"]="0";enabled=false;break;
          case 4: env_ctrl_exp_from_params=true;enabled=false;break;
          case 5: dual=false;enabled=false;break;
        }
      }
      const int prior_model_poll=polled["modelV2"],prior_base_poll=polled["liveCalibration"];
      for(const auto& name:names)send(publishers[name],i);
      context.update(dual);assert(context.ready==enabled);
      if(!enabled)assert(polled["liveCalibration"]==prior_base_poll);
      // This check applies to the revised separate lazy subscriber; the old
      // baseline can keep polling modelV2 only until its next reconstruction.
      if(values["Pv5RoadLaneExposure"]=="0")assert(polled["modelV2"]==prior_model_poll);
      for(const auto& name:names){
        if(receive(witnesses[name][0])!=static_cast<uint32_t>(i)){++losses;++lost_by_topic[name];}
        for(int k=1;k<3;++k)msgq_msg_ready(&witnesses[name][k]);
      }
    }
    stress_masters=masters;stress_cal=created["liveCalibration"];stress_model=created["modelV2"];
    // Exercise retained subscriptions after the transition stress, not just a
    // newly-created context. Gates and exact age boundaries must still apply.
    auto healthy=[&](){
      clock_now+=1100000000ULL;cal_time=dev_time=clock_now;
      values["Pv5ModelFovExposure"]="1";values["Pv5RoadLaneExposure"]="1";values["UseWideCamera"]="1";
      env_ctrl_exp_from_params=false;pv5=true;not_car=false;messages_valid=true;model_valid=true;
      model_age=model_event_age=0;angles={0,0,0};calibrated=cereal::LiveCalibrationData::Status::CALIBRATED;
      device=cereal::InitData::DeviceType::TIZI;lane_change=cereal::LaneChangeState::OFF;
      context.update(true);assert(context.ready);
    };
    healthy();cal_time=clock_now-1000000001ULL;context.update(true);assert(!context.ready);++guard_checks;
    healthy();dev_time=clock_now-2500000001ULL;context.update(true);assert(!context.ready);++guard_checks;
    healthy();cal_time=clock_now+1;context.update(true);assert(!context.ready);++guard_checks;
    healthy();messages_valid=false;context.update(true);assert(!context.ready);++guard_checks;
    healthy();calibrated=cereal::LiveCalibrationData::Status::UNCALIBRATED;context.update(true);assert(!context.ready);++guard_checks;
    healthy();angles[0]=std::nan("");context.update(true);assert(!context.ready);++guard_checks;
    healthy();device=cereal::InitData::DeviceType::MICI;context.update(true);assert(!context.ready);++guard_checks;
    healthy();pv5=false;clock_now+=1100000000ULL;cal_time=dev_time=clock_now;context.update(true);assert(!context.ready);++guard_checks;
    healthy();not_car=true;clock_now+=1100000000ULL;cal_time=dev_time=clock_now;context.update(true);assert(!context.ready);++guard_checks;
    for(int fault=0;fault<5;++fault){
      healthy();model_valid=fault!=0;model_age=fault==1?250000001ULL:0;model_event_age=fault==2?200000001ULL:0;
      if(fault==3)lane_change=cereal::LaneChangeState::STARTING;
      if(fault==4)model_event_age=uint64_t(-1); // event timestamp is in the future
      context.update(true);assert(context.ready && !context.boxes[0].valid);++guard_checks;
    }
    healthy();values["CameraYawTrimDeg"]="100";clock_now+=300000000ULL;context.update(true);assert(!context.ready);++guard_checks;
    values["CameraYawTrimDeg"]="0";clock_now+=300000000ULL;context.update(true);assert(!context.ready);++guard_checks;
    for(const auto& name:names)active_slots+=queues[name].readers.load();
  }
  assert(live_subs==0);
  std::cout<<"{\"masters\":"<<masters<<",\"calibration_subscriptions\":"<<created["liveCalibration"]
   <<",\"device_subscriptions\":"<<created["deviceState"]<<",\"model_subscriptions\":"<<created["modelV2"]
   <<",\"lost_pending_samples\":"<<losses<<",\"slots_after_stress_and_guards\":"<<active_slots
   <<",\"stress_masters\":"<<stress_masters<<",\"stress_calibration\":"<<stress_cal<<",\"stress_model\":"<<stress_model
   <<",\"lost_calibration\":"<<lost_by_topic["liveCalibration"]
   <<",\"active_subscribers_after_destruction\":"<<live_subs<<",\"guard_checks\":"<<guard_checks<<"}\n";
}
'''


def build_native(folder, context_source=None):
  fixture = Path(__file__).with_name('model_fov_context_fixture.h').read_text()
  marker = 'struct SubMaster {'
  fixture = fixture.replace(marker, NATIVE + marker + '\n  std::map<std::string,msgq_queue_t> sockets;')
  old = 'explicit SubMaster(std::vector<const char *>) {}'
  assert old in fixture
  fixture = fixture.replace(old, '''explicit SubMaster(std::vector<const char *> names) {
    ++masters;for(const auto& name:names){++created[name];++live_subs;sockets[name]=queues[name].queue();msgq_init_subscriber(&sockets[name]);}
  }
  ~SubMaster(){for(auto& entry:sockets){msgq_close_queue(&entry.second);--live_subs;}}''')
  fixture = fixture.replace('void update(int timeout) { assert(timeout == 0); }', '''void update(int timeout) {
    assert(timeout==0);for(auto& entry:sockets){++polled[entry.first];receive(entry.second);}
  }''')
  source = context_source or (ROOT / 'openpilot/system/camerad/cameras/camera_qcom2.cc').read_text()
  context = 'class ModelFovContext' + source.split('class ModelFovContext', 1)[1].split('// Disk reads', 1)[0]
  wrapper, executable = folder / 'context-lifecycle.cc', folder / 'context-lifecycle'
  wrapper.write_text(fixture + context + SCENARIOS)
  result = subprocess.run(['g++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                           '-I', str(ROOT / 'openpilot'), '-I', str(ROOT / 'msgq_repo'), str(wrapper),
                           str(ROOT / 'msgq_repo/msgq/msgq.cc'), '-o', str(executable)], capture_output=True, text=True)
  assert result.returncode == 0, result.stderr
  return executable


@pytest.fixture(scope='module')
def executable(tmp_path_factory):
  return build_native(tmp_path_factory.mktemp('exposure-ipc'))


@pytest.mark.parametrize('mode', ['fixed', 'audit100', 'lane', 'power', 'mixed'])
def test_reuse_preserves_other_receivers_and_safety_gates(executable, mode):
  result = subprocess.run([str(executable), mode], capture_output=True, text=True, timeout=10)
  assert result.returncode == 0, result.stderr
  counts = json.loads(result.stdout.strip().splitlines()[-1])
  # Safety checks deliberately enable lane metering once after stress even in
  # fixed/base-only scenarios. No topic is subscribed more than once.
  assert counts == dict(masters=2, calibration_subscriptions=1, device_subscriptions=1,
                       model_subscriptions=1, lost_pending_samples=0,
                       slots_after_stress_and_guards=12, active_subscribers_after_destruction=0, guard_checks=16,
                       stress_masters=1 if mode in ('fixed', 'power') else 2, stress_calibration=1,
                       stress_model=0 if mode in ('fixed', 'power') else 1, lost_calibration=0)
