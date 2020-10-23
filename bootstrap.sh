#!/bin/bash

# create the virtual environment
python -m venv .env
# download requirements
.env/bin/python -m pip install -r requirements.txt
# create the key pair
aws ec2 create-key-pair --key-name elk-key-pair --query 'KeyMaterial' --output text > elk-key-pair.pem --region us-east-1
# update key_pair permissions
chmod 400 elk-key-pair.pem
# move key_pair to .ssh
mv -f elk-key-pair.pem $HOME/.ssh/elk-key-pair.pem
# start the ssh agent
eval `ssh-agent -s`
# add your key to keychain
ssh-add -k ~/.ssh/elk-key-pair.pem 