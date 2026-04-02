#!/bin/bash
set -e

rm -rf _mcpb_build thermy.mcpb

mkdir -p _mcpb_build/server/lib
cp manifest.json _mcpb_build/
cp server/main.py _mcpb_build/server/
cp thermy.py _mcpb_build/server/lib/

cd _mcpb_build
zip -r ../thermy.mcpb .
cd ..
rm -rf _mcpb_build

echo "Built thermy.mcpb"
unzip -l thermy.mcpb
