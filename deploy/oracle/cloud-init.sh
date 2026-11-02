#!/bin/bash
# Paste this into Oracle's "Initialization script" box when creating the server.
# Change the password on the next line first (don't use a single quote ' in it).
export FT_PASSWORD='choose-a-password'
curl -fsSL https://raw.githubusercontent.com/cchigu/flight-price-tracker/main/deploy/oracle/setup.sh | bash
