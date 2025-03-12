# Import the CloudLab library
import geni.portal as portal
import geni.rspec.pg as rspec

# Create a request object
request = portal.Context().makeRequestRSpec()

# Define a single node
node = request.RawPC("node")
node.hardware_type = "c8220"  # Use c8220 machine
node.disk_image = "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU20-64-STD"

# Print the final RSpec
portal.Context().printRequestRSpec()
