{ lib, python3Packages }:

python3Packages.buildPythonApplication {
  pname = "btlog";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ../.;

  build-system = [ python3Packages.setuptools ];
  # bleak talks to BlueZ over D-Bus; it is the only dependency.
  dependencies = [ python3Packages.bleak ];

  # The tests use a scripted scanner source and a web server on 127.0.0.1,
  # which the build sandbox allows; no Bluetooth hardware is involved.
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    PYTHONPATH=src:$PYTHONPATH python -m unittest discover -s tests -v
    runHook postCheck
  '';
  pythonImportsCheck = [ "btlog.cli" ];

  meta = {
    description = "Always-on log of Bluetooth LE devices in proximity, with trend analysis";
    license = lib.licenses.mit;
    mainProgram = "btlog";
    platforms = lib.platforms.linux;
  };
}
