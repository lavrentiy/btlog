# NixOS module: services.btlog
#
#   services.btlog = {
#     enable = true;
#     mode = "home";             # home, sensors, footfall or record: a set of defaults
#     interval = 60;             # seconds per scan cycle
#     retentionDays = 365;       # raw rows; 0 = forever (hourly rollups always are); null = the mode's
#     forgetRandomDays = 7;      # drop rotating private addresses unseen this long; 0 = never; null = the mode's
#     web.enable = true;         # read-only dashboard on http://127.0.0.1:8778
#   };
#
# Only options that are set land in /etc/btlog.toml next to the mode: a key
# written there beats the mode, so an option with a default of its own would
# quietly undo the mode's (record mode's 0 for "keep forever", say).
#
# The scanner needs a Bluetooth adapter managed by BlueZ (hardware.bluetooth
# is enabled by this module) and nothing else: it talks to BlueZ over the
# system D-Bus, whose default policy lets any user start discovery. If your
# BlueZ is configured more strictly, add the btlog user to its allowed group.
#
# The dashboard has no authentication: leave web.bind on localhost and reach
# it over an SSH port forward or Tailscale, or bind it to a trusted network.

{ config, lib, pkgs, ... }:

let
  cfg = config.services.btlog;
  toml = pkgs.formats.toml { };
  configFile = toml.generate "btlog.toml" ({
    db = "${cfg.stateDir}/btlog.db";
    mode = cfg.mode;
    interval = cfg.interval;
    web_bind = cfg.web.bind;
    web_names = cfg.web.names;
  } // lib.filterAttrs (_: v: v != null) {
    adapter = cfg.adapter;
    retention_days = cfg.retentionDays;
    forget_random_days = cfg.forgetRandomDays;
    payloads = cfg.payloads;
  });
  hardening = {
    NoNewPrivileges = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectKernelLogs = true;
    ProtectControlGroups = true;
    ProtectClock = true;
    ProtectHostname = true;
    ProtectProc = "invisible";
    RestrictNamespaces = true;
    RestrictRealtime = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    MemoryDenyWriteExecute = true;
    SystemCallArchitectures = "native";
    SystemCallFilter = [ "@system-service" "~@privileged" ];
    CapabilityBoundingSet = "";
    UMask = "0077";
  };
  # systemd creates the state directory only under /var/lib; elsewhere the
  # directory has to exist already.
  stateDirectory = lib.mkIf (lib.hasPrefix "/var/lib/" cfg.stateDir) (lib.removePrefix "/var/lib/" cfg.stateDir);
in
{
  options.services.btlog = {
    enable = lib.mkEnableOption "btlog, an always-on log of Bluetooth LE devices in proximity";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "btlog from its flake";
    };

    interval = lib.mkOption {
      type = lib.types.numbers.positive;
      default = 60;
      description = "Seconds per scan cycle. Each cycle records at most one sighting per device.";
    };

    adapter = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "hci1";
      description = "Adapter to scan with; null is the default adapter.";
    };

    mode = lib.mkOption {
      type = lib.types.enum [ "home" "sensors" "footfall" "record" ];
      default = "home";
      description = ''
        What btlog is set up for; each mode is a set of defaults for the
        options below that are left at null. home: every device, raw data for
        90 days, rotating addresses forgotten after 7. sensors: decoded sensors
        only, everything else is dropped before it is written. footfall:
        rotating addresses only counted per hour, never stored; raw data for
        30 days. record: everything kept forever, raw payloads too, which in
        most places makes you responsible for other people's personal data
        (see the README's Modes section).
      '';
    };

    retentionDays = lib.mkOption {
      type = lib.types.nullOr lib.types.ints.unsigned;
      default = null;
      example = 365;
      description = ''
        How long raw sightings, readings and payloads are kept; 0 keeps them
        forever. Hourly rollups are kept forever. null: the mode's default
        (90 days; 30 in footfall mode, forever in record mode).
      '';
    };

    forgetRandomDays = lib.mkOption {
      type = lib.types.nullOr lib.types.ints.unsigned;
      default = null;
      example = 7;
      description = ''
        Rotating private addresses not seen for this many days are removed from
        the database, keeping only their contribution to the per-hour counts.
        0 never removes them. null: the mode's default (7; never in record mode).
      '';
    };

    payloads = lib.mkOption {
      type = lib.types.nullOr lib.types.bool;
      default = null;
      description = ''
        Also store the raw manufacturer and service data bytes of every
        device, per scan cycle, for evidence and for decoding later; they
        follow retentionDays and roughly double the database. null: the
        mode's default (true in record mode only).
      '';
    };

    waitForTimeSync = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Start scanning only once the clock is synchronized over the network,
        by enabling systemd-time-wait-sync (for systemd-timesyncd, the NixOS
        default). Worth it on a board without a battery-backed clock, where
        cycles logged before NTP lands under the wrong hours for good. The
        price: on a machine that never gets network, the daemon never starts,
        and neither does anything else ordered after time-sync.target (timers
        with OnCalendar= among them). To wait at most a while and then scan
        anyway, also set
        systemd.services.systemd-time-wait-sync.serviceConfig.TimeoutStartSec.
      '';
    };

    stateDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/btlog";
    };

    web.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Run `btlog web`, a read-only dashboard over the database, as the
        separate service btlog-web. It has no authentication.
      '';
    };

    web.bind = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1:8778";
      description = ''
        HOST:PORT the dashboard listens on. Keep it on localhost (reach it over an
        SSH port forward or Tailscale) or bind it to a trusted network only.
      '';
    };

    web.names = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "bench.tailnet-1234.ts.net" ];
      description = ''
        Extra host names the dashboard answers to. It always answers to IP
        addresses, localhost and, when bound beyond localhost, this machine's
        hostname, FQDN and hostname.local; any other name gets a 421 so a web
        page cannot rebind its own name to this machine. A Tailscale MagicDNS
        name is the usual entry here.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    hardware.bluetooth.enable = lib.mkDefault true;
    environment.etc."btlog.toml".source = configFile;
    environment.systemPackages = [ cfg.package ];

    users.users.btlog = {
      isSystemUser = true;
      group = "btlog";
      description = "btlog scanner";
    };
    users.groups.btlog = { };

    # NixOS ships systemd-time-wait-sync but not enabled: add it and hook it
    # into sysinit.target as its upstream [Install] section would.
    systemd.additionalUpstreamSystemUnits = lib.mkIf cfg.waitForTimeSync [ "systemd-time-wait-sync.service" ];
    systemd.services.systemd-time-wait-sync = lib.mkIf cfg.waitForTimeSync { wantedBy = [ "sysinit.target" ]; };

    systemd.services.btlog = {
      description = "Log of Bluetooth LE devices in proximity";
      wantedBy = [ "multi-user.target" ];
      # Ordering only: time-sync.target waits for NTP just when something
      # delays it, which waitForTimeSync arranges. Consumers must not pull it in.
      after = [ "bluetooth.service" "time-sync.target" ];
      requires = [ "bluetooth.service" ];
      restartTriggers = [ configFile ];
      serviceConfig = hardening // {
        ExecStart = "${lib.getExe cfg.package} daemon";
        User = "btlog";
        Group = "btlog";
        Restart = "always";
        RestartSec = 5;
        StateDirectory = stateDirectory;
        StateDirectoryMode = "0750";
        ReadWritePaths = [ cfg.stateDir ];
        # Only the system D-Bus socket, which is a path, not a network address.
        RestrictAddressFamilies = [ "AF_UNIX" ];
        PrivateNetwork = true;
      };
    };

    # The dashboard opens the database read-only, but runs as the same user:
    # SQLite's WAL mode needs readers to share the daemon's -shm file, which
    # a throwaway user could not open.
    systemd.services.btlog-web = lib.mkIf cfg.web.enable {
      description = "btlog web dashboard (read-only, no authentication)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "btlog.service" ];
      wants = [ "btlog.service" ];
      restartTriggers = [ configFile ];
      serviceConfig = hardening // {
        ExecStart = "${lib.getExe cfg.package} web --bind ${lib.escapeShellArg cfg.web.bind}";
        User = "btlog";
        Group = "btlog";
        Restart = "always";
        RestartSec = 5;
        StateDirectory = stateDirectory;
        StateDirectoryMode = "0750";   # systemd re-applies the mode on every start
        ReadWritePaths = [ cfg.stateDir ];
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
      };
    };
  };
}
