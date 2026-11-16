#undef NDEBUG
#include <cassert>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <filesystem>
#include <functional>
#include <iostream>
#include <memory>
#include <numeric>
#include <thread>
#include <unistd.h>

#include "system/loggerd/batch_poller.h"

using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
static volatile sig_atomic_t cancelled = 0;
static volatile sig_atomic_t interruptions = 0;
#ifdef COUNT_READY_CALLS
extern thread_local uint64_t test_ready_calls;
#endif

struct CountingPoller : MSGQPoller {
  size_t calls = 0;
  std::function<void()> after_scan;
  std::vector<SubSocket *> poll(int timeout) override {
    ++calls;
    auto ready = MSGQPoller::poll(timeout);
    if (after_scan) { auto fn = std::move(after_scan); after_scan = {}; fn(); }
    return ready;
  }
};

struct Namespace {
  std::string prefix, path;
  std::vector<std::string> names;
  Namespace() {
    static int counter = 0;
    prefix = "pv5_logger_batch_test_" + std::to_string(getpid()) + "_" + std::to_string(counter++);
    path = "/dev/shm/msgq_" + prefix;
    assert(std::filesystem::create_directory(path));
    setenv("OPENPILOT_PREFIX", prefix.c_str(), 1);
  }
  ~Namespace() {
    for (const auto &name : names) assert(unlink((path + "/" + name).c_str()) == 0);
    assert(rmdir(path.c_str()) == 0);
  }
};

struct Queues {
  Namespace owned;
  Context context;
  CountingPoller poller;
  LoggerdBatchPoller batched{poller};
  std::vector<std::unique_ptr<PubSocket>> pubs;
  std::vector<std::unique_ptr<MSGQSubSocket>> subs;
  Queues(int count, size_t size=250*1024) : Queues(std::vector<size_t>(count,size)) {}
  explicit Queues(const std::vector<size_t> &sizes) {
    const int count=sizes.size();
    for (int i=0; i<count; ++i) {
      const auto size=sizes[i];
      auto name = "q" + std::to_string(i);
      owned.names.push_back(name);
      auto pub = std::make_unique<PubSocket>();
      assert(pub->connect(&context, name, true, size) == 0);
      auto sub = std::make_unique<MSGQSubSocket>();
      assert(sub->connect(&context, name, "127.0.0.1", false, true, size) == 0);
      poller.registerSocket(sub.get());
      pubs.push_back(std::move(pub)); subs.push_back(std::move(sub));
    }
  }
  std::vector<SubSocket *> poll(bool batch, int timeout=1000) {
    return batch ? batched.poll(timeout, []{return cancelled != 0;}) : poller.poll(timeout);
  }
};

struct Payload {
  uint32_t stream, seq, segment, size;
  uint64_t stamp;
  std::array<uint8_t, 232> body;
};
static_assert(sizeof(Payload)==256);
Payload packet(uint32_t stream, uint32_t seq) {
  Payload p{stream,seq,seq/7,sizeof(Payload),uint64_t(seq)*1000000+stream,{}};
  for (size_t j=0;j<p.body.size();++j) p.body[j] = (stream*11+seq*7+j)%251;
  return p;
}
void send(Queues &q, size_t stream, uint32_t seq) {
  auto p=packet(stream,seq);
  assert(q.pubs[stream]->send(reinterpret_cast<char *>(&p),sizeof(p))==sizeof(p));
}
void verify(Message *message, std::vector<uint32_t> &next, std::vector<uint64_t> &hashes) {
  assert(message->getSize()==sizeof(Payload));
  Payload p; std::memcpy(&p,message->getData(),sizeof(p));
  assert(p.stream<next.size() && p.seq==next[p.stream]++);
  auto expected=packet(p.stream,p.seq);
  assert(std::memcmp(&p,&expected,sizeof(p))==0);
  hashes[p.stream] = hashes[p.stream]*1099511628211ULL + p.seq*3+p.stamp+p.segment;
}
uint64_t cpu_ns() { timespec t; assert(clock_gettime(CLOCK_THREAD_CPUTIME_ID,&t)==0); return uint64_t(t.tv_sec)*1000000000+t.tv_nsec; }
bool same_mask(const sigset_t &a,const sigset_t &b) { for(int i=1;i<NSIG;i++) if(sigismember(&a,i)!=sigismember(&b,i))return false;return true; }
sigset_t mask() { sigset_t out; assert(pthread_sigmask(SIG_SETMASK,nullptr,&out)==0);return out; }

struct BenchResult {size_t messages,calls,headers;uint64_t cpu;double wall;std::vector<uint64_t> hashes;};
BenchResult benchmark(bool batch) {
  constexpr int streams=96, rounds=32;
  Queues q(streams);
  std::vector<uint32_t> next(streams,0);
  std::vector<uint64_t> hashes(streams,0);
  const auto start=Clock::now();
  auto producer=std::thread([&] {
    // Same 1,500 messages/s and payloads in both modes, not a tight-loop throughput race.
    for(int i=0;i<streams*rounds;i++) {
      std::this_thread::sleep_until(start+std::chrono::microseconds(int64_t(i+1)*1000000/1500));
      send(q,i%streams,i/streams);
    }
  });
#ifdef COUNT_READY_CALLS
  test_ready_calls=0;
#endif
  size_t received=0;
  const auto cpu=cpu_ns();
  while(received<streams*rounds && Clock::now()-start<5s) {
    for(auto socket:q.poll(batch)) {
      for(int count=0;count<200;count++) {
        std::unique_ptr<Message> message(socket->receive(true));
        if(!message)break;
        verify(message.get(),next,hashes);++received;
      }
    }
  }
  const auto used=cpu_ns()-cpu;
  producer.join();
  assert(received==streams*rounds);
  size_t headers=0;
#ifdef COUNT_READY_CALLS
  headers=test_ready_calls;
#endif
  return {received,q.poller.calls,headers,used,std::chrono::duration<double>(Clock::now()-start).count(),hashes};
}

void burst_and_segment_order(bool batch) {
  Queues q(8);
  for(int seq=0;seq<300;seq++)for(int stream=0;stream<8;stream++)send(q,stream,seq);
  std::vector<uint32_t> next(8,0);std::vector<uint64_t> hashes(8,0);int received=0;
  const auto start=Clock::now();
  while(received<2400 && Clock::now()-start<1s) for(auto socket:q.poll(batch,100))for(int n=0;n<200;n++){
    std::unique_ptr<Message> m(socket->receive(true));if(!m)break;verify(m.get(),next,hashes);received++;
  }
  assert(received==2400); // >200 per queue exercises the logger's existing fair-drain cap.
}

void saturation_recovers_without_corruption(bool batch) {
  Queues q(1,4096);
  for(int i=0;i<100;i++)send(q,0,i);
  // Existing msgq overrun invalidates this reader. Batching must not invent data.
  auto lost=q.poll(batch,0);assert(lost.empty());
  send(q,0,100);
  auto ready=q.poll(batch,100);assert(ready.size()==1);
  std::unique_ptr<Message> m(ready[0]->receive(true));assert(m && m->getSize()==sizeof(Payload));
  auto expected=packet(0,100);assert(std::memcmp(m->getData(),&expected,sizeof(expected))==0);
}

void publish_between_scan_and_wait_is_not_lost() {
  Queues q(1);auto before=mask();
  q.poller.after_scan=[&]{send(q,0,0);};
  const auto start=Clock::now();auto ready=q.poll(true,1000);
  assert(ready.size()==1 && Clock::now()-start<150ms);
  assert(same_mask(before,mask()));
  delete ready[0]->receive(true);
}

void shutdown_during_wait(int signal,bool in_batch,bool at_scan_boundary) {
  Queues q(1);cancelled=0;auto before=mask();
  if(in_batch){send(q,0,0);auto ready=q.poll(true);delete ready[0]->receive(true);}
  const auto owner=pthread_self();
  std::thread killer;
  if(at_scan_boundary)q.poller.after_scan=[&]{pthread_kill(owner,signal);};
  else killer=std::thread([&]{std::this_thread::sleep_for(2ms);pthread_kill(owner,signal);});
  const auto start=Clock::now();assert(q.poll(true,1000).empty());
  assert(cancelled && Clock::now()-start<150ms);
  if(killer.joinable())killer.join();
  assert(same_mask(before,mask()));cancelled=0;
}

void unrelated_signal_storm_respects_timeout_and_spacing() {
  Queues q(1);const auto owner=pthread_self();std::atomic<bool> done=false;
  auto storm=std::thread([&]{while(!done){pthread_kill(owner,SIGUSR1);std::this_thread::sleep_for(1ms);}});
  auto before=mask();const auto start=Clock::now();auto ready=q.poll(true,60);const auto elapsed=Clock::now()-start;
  done=true;storm.join();assert(ready.empty() && elapsed>=55ms && elapsed<200ms);
  assert(q.poller.calls<=7 && interruptions>0 && same_mask(before,mask()));
}

struct AlternatePoller : Poller {
  int seen=-99;
  void registerSocket(SubSocket *) override {}
  std::vector<SubSocket *> poll(int timeout) override {seen=timeout;return {};}
};
void alternate_backend_retains_original_wait() {AlternatePoller p;LoggerdBatchPoller b(p);b.poll(37,[]{return false;});assert(p.seen==37);}

void exception_restores_mask() {
  Queues q(1);auto before=mask();q.poller.after_scan=[]{throw std::runtime_error("owned test exception");};
  bool caught=false;try{q.poll(true,100);}catch(const std::runtime_error &){caught=true;}
  assert(caught && same_mask(before,mask()));
}

void long_drain_consumes_batch_interval() {
  Queues q(1);send(q,0,0);auto ready=q.poll(true);delete ready[0]->receive(true);
  std::this_thread::sleep_for(30ms);send(q,0,1);const auto start=Clock::now();ready=q.poll(true);
  assert(ready.size()==1 && Clock::now()-start<8ms);delete ready[0]->receive(true);
}

void original_blocked_mask_retains_backend_behavior() {
  Queues q(1);
  auto original=mask();
  sigset_t blocked=original;
  sigaddset(&blocked,SIGUSR2);sigaddset(&blocked,SIGUSR1);
  assert(pthread_sigmask(SIG_SETMASK,&blocked,nullptr)==0);
  auto producer=std::thread([&]{std::this_thread::sleep_for(2ms);send(q,0,0);});
  auto ready=q.poll(true,30);producer.join();
  assert(ready.size()==1 && q.poller.calls==1 && same_mask(blocked,mask()));
  delete ready[0]->receive(true);
  assert(pthread_sigmask(SIG_SETMASK,&original,nullptr)==0);
}

void extra_blocked_signal_is_preserved() {
  Queues q(1);auto original=mask();auto blocked=original;sigaddset(&blocked,SIGUSR1);
  assert(pthread_sigmask(SIG_SETMASK,&blocked,nullptr)==0);
  q.poller.after_scan=[&]{send(q,0,0);};
  auto ready=q.poll(true,100);assert(ready.size()==1 && same_mask(blocked,mask()));
  delete ready[0]->receive(true);
  assert(pthread_sigmask(SIG_SETMASK,&original,nullptr)==0);
}
std::vector<char> mixed_packet(int stream,int seq) {
  const size_t size=stream==0 ? 256 : 512*1024;
  std::vector<char> out(size);
  uint32_t header[]={uint32_t(stream),uint32_t(seq),uint32_t(size)};
  std::memcpy(out.data(),header,sizeof(header));
  for(size_t i=sizeof(header);i<size;i++)out[i]=(i*7+seq*13+stream)%251;
  return out;
}

void mixed_large_and_small_payloads(bool batch) {
  // Configured production queue capacities; synthetic payload/rates are test
  // conditions, not a measured upper bound for real encoder output or load.
  Queues q(std::vector<size_t>{250*1024,10*1024*1024});
  const auto start=Clock::now();
  auto producer=std::thread([&]{
    for(int i=0;i<100;i++) {
      std::this_thread::sleep_until(start+std::chrono::milliseconds((i+1)*10));
      auto small=mixed_packet(0,i);
      assert(q.pubs[0]->send(small.data(),small.size())==small.size());
      if(i%5==0) {
        auto large=mixed_packet(1,i/5);
        assert(q.pubs[1]->send(large.data(),large.size())==large.size());
      }
    }
  });
  std::array<uint32_t,2> next{};size_t bytes=0;
  while(next[0]+next[1]<120 && Clock::now()-start<3s) {
    for(auto socket:q.poll(batch,100))for(int n=0;n<200;n++) {
      std::unique_ptr<Message> m(socket->receive(true));if(!m)break;
      assert(m->getSize()>=12);uint32_t header[3];std::memcpy(header,m->getData(),sizeof(header));
      assert(header[0]<2 && header[1]==next[header[0]]++);
      auto expected=mixed_packet(header[0],header[1]);
      assert(m->getSize()==expected.size() && std::memcmp(m->getData(),expected.data(),expected.size())==0);
      bytes+=m->getSize();
    }
  }
  producer.join();assert(next[0]==100 && next[1]==20 && bytes==10511360);
}
int main() {
  struct sigaction stop={},noise={},previous_noise={};
  constexpr std::array<int,3> stop_signals={SIGINT,SIGTERM,SIGPWR};
  std::array<struct sigaction,3> previous_stop{};
  stop.sa_handler=[](int){cancelled=1;};sigemptyset(&stop.sa_mask);for(size_t i=0;i<stop_signals.size();i++)assert(sigaction(stop_signals[i],&stop,&previous_stop[i])==0);
  noise.sa_handler=[](int){interruptions=1;};sigemptyset(&noise.sa_mask);assert(sigaction(SIGUSR1,&noise,&previous_noise)==0);
  burst_and_segment_order(false);burst_and_segment_order(true);
  saturation_recovers_without_corruption(false);saturation_recovers_without_corruption(true);
    publish_between_scan_and_wait_is_not_lost();
  for(auto signal:stop_signals){shutdown_during_wait(signal,false,false);shutdown_during_wait(signal,true,false);shutdown_during_wait(signal,false,true);}
  original_blocked_mask_retains_backend_behavior();extra_blocked_signal_is_preserved();
  unrelated_signal_storm_respects_timeout_and_spacing();alternate_backend_retains_original_wait();exception_restores_mask();long_drain_consumes_batch_interval();
  mixed_large_and_small_payloads(false);mixed_large_and_small_payloads(true);
  auto baseline=benchmark(false),batch=benchmark(true);
  assert(baseline.hashes==batch.hashes);
  assert(batch.calls<baseline.calls/3);
  std::cout << "{\"cases\":24,\"passed\":24,\"baseline\":{\"messages\":"<<baseline.messages<<",\"poll_calls\":"<<baseline.calls<<",\"ready_header_checks\":"<<baseline.headers<<",\"cpu_ns\":"<<baseline.cpu<<",\"wall_s\":"<<baseline.wall<<"},\"batched\":{\"messages\":"<<batch.messages<<",\"poll_calls\":"<<batch.calls<<",\"ready_header_checks\":"<<batch.headers<<",\"cpu_ns\":"<<batch.cpu<<",\"wall_s\":"<<batch.wall<<"},\"per_queue_order_content_and_segment_tags_equal\":true,\"forced_overrun_policy_unchanged\":true,\"mixed_payload_messages_per_mode\":120,\"mixed_payload_bytes_per_mode\":10511360}\n";
  for(size_t i=0;i<stop_signals.size();i++)sigaction(stop_signals[i],&previous_stop[i],nullptr);sigaction(SIGUSR1,&previous_noise,nullptr);
}
