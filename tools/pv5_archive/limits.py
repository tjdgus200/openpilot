"""Apply limits inside a dedicated PC analysis process, before parsing inputs."""

import os

_job = None


def apply():
  global _job
  maximum = 384 * 1024**2
  if os.name != 'nt':
    import resource

    os.nice(19)
    resource.setrlimit(resource.RLIMIT_AS, (maximum, maximum))
    resource.setrlimit(resource.RLIMIT_CPU, (95, 95))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return
  import ctypes as c
  from ctypes import wintypes as w

  class Basic(c.Structure):
    _fields_ = [
      ('process_time', c.c_int64),
      ('job_time', c.c_int64),
      ('flags', w.DWORD),
      ('min_working', c.c_size_t),
      ('max_working', c.c_size_t),
      ('active', w.DWORD),
      ('affinity', c.c_size_t),
      ('priority', w.DWORD),
      ('scheduling', w.DWORD),
    ]

  class Io(c.Structure):
    _fields_ = [(name, c.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read', 'write', 'other')]

  class Extended(c.Structure):
    _fields_ = [
      ('basic', Basic),
      ('io', Io),
      ('process_memory', c.c_size_t),
      ('job_memory', c.c_size_t),
      ('peak_process', c.c_size_t),
      ('peak_job', c.c_size_t),
    ]

  kernel = c.WinDLL('kernel32', use_last_error=True)
  kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [c.c_void_p, w.LPCWSTR], w.HANDLE
  kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
  kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
  kernel.GetCurrentProcess.restype = w.HANDLE
  kernel.SetPriorityClass.argtypes = [w.HANDLE, w.DWORD]
  _job = kernel.CreateJobObjectW(None, None)
  info = Extended()
  info.basic.flags = 0x100 | 0x8 | 0x2  # PROCESS_MEMORY, ACTIVE_PROCESS, PROCESS_TIME
  info.basic.active, info.basic.process_time, info.process_memory = 1, 95 * 10000000, maximum
  if (
    not _job
    or not kernel.SetInformationJobObject(_job, 9, c.byref(info), c.sizeof(info))
    or not kernel.AssignProcessToJobObject(_job, kernel.GetCurrentProcess())
    or not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000)
  ):
    raise RuntimeError('pc_resource_limit_unavailable')
