# Import the CloudLab library
import geni.portal as portal
import geni.rspec.pg as rspec

# Create a request object
request = portal.Context().makeRequestRSpec()

# Define node 1
node1 = request.RawPC("node1")
node1.hardware_type = "c8220"  # Use c8220 machine
node1.disk_image = "urn:publicid:IDN+emulab.net+image+Ubuntu20-04"

# Define node 2
node2 = request.RawPC("node2")
node2.hardware_type = "c8220"  # Use c8220 machine
node2.disk_image = "urn:publicid:IDN+emulab.net+image+Ubuntu20-04"

# Create a network link between the two nodes
link = request.Link(members=[node1, node2])

# Print the final RSpec
portal.Context().printRequestRSpec()
