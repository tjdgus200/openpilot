"""Boot-only PV5 build repair admission; no native Params or driving-process imports."""
from pathlib import Path
import sys


MAX_CAR_PARAMS_BYTES = 1024 * 1024


def pv5_build_recovery_enabled(params_dir: Path = Path("/data/params/d")) -> bool:
  """Use the last identified real car only for build maintenance, never vehicle control.

  Missing/corrupt/unsupported persisted data retains the original startup/build path.
  Loading only the schema works before native Params has been rebuilt.
  """
  try:
    with (params_dir / "CarParamsPersistent").open("rb") as stream:
      raw = stream.read(MAX_CAR_PARAMS_BYTES + 1)
    if len(raw) < 16 or len(raw) % 8 or len(raw) > MAX_CAR_PARAMS_BYTES:
      return False
    import capnp
    schema_dir = Path(__file__).resolve().parents[3] / "opendbc_repo/opendbc/car"
    schema = capnp.SchemaParser().load(str(schema_dir / "car.capnp"), imports=[str(schema_dir)])
    with schema.CarParams.from_bytes(raw, traversal_limit_in_words=MAX_CAR_PARAMS_BYTES // 8) as cp:
      return cp.brand == "hyundai" and cp.carFingerprint == "KIA_PV5" and not cp.notCar
  except Exception:
    # A damaged build/dependency must not turn an unknown car into a PV5 repair target.
    return False


if __name__ == "__main__":
  sys.exit(0 if pv5_build_recovery_enabled() else 1)
