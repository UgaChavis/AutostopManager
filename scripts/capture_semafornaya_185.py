#!/usr/bin/env python3
"""Compatibility wrapper for the allowlisted AutoStop Semafornaya 185 view."""

from autostop_manager.public_camera import main


if __name__ == "__main__":
    raise SystemExit(main(default_camera_key="semafornaya-185"))
